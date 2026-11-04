// Run a vendor ONNX model with deterministic file input and one explicit backend.
// Alnpu is the default; ALRef/CpuRef are diagnostics, never implicit fallbacks.
// This checks inference without requiring a camera/display demo pipeline.
#include <armnn/ArmNN.hpp>
#include <armnn/IProfiler.hpp>
#include <armnnOnnxParser/IOnnxParser.hpp>
#include <armnnALUtils/cma_mem_init.hpp>
#include <chrono>
#include <fstream>
#include <iostream>
#include <memory>
#include <vector>
#include "tensor_io.hpp"

struct CmaBuffer {
    cma_buffer_t data;
    explicit CmaBuffer(unsigned bytes):data(bytes) {
        data.flags=0; cma_mem_alloc(&data);
        if(!data.virt_addr || !data.phys_addr) throw std::runtime_error("CMA allocation failed");
    }
    ~CmaBuffer(){if(data.phys_addr)cma_mem_free(&data);}
    CmaBuffer(const CmaBuffer&)=delete;
    CmaBuffer& operator=(const CmaBuffer&)=delete;
};

int main(int argc,char** argv) try {
    armnn::ConfigureLogging(true,false,armnn::LogSeverity::Debug);
    if(argc<4 || argc>5){std::cerr<<"Usage: vendor_model_probe MODEL INPUT_F32 OUTPUT_PREFIX [Alnpu|ALRef|CpuRef]\n";return 2;}
    const std::string backend=argc==5?argv[4]:"Alnpu";
    if(backend!="Alnpu" && backend!="ALRef" && backend!="CpuRef")throw std::runtime_error("Unknown diagnostic backend");
    std::cout<<"requested_backend="<<backend<<" fallback=disabled"<<std::endl;
    const std::string prefix=argv[3];
    auto parser=armnnOnnxParser::IOnnxParser::Create();
    auto network=parser->CreateNetworkFromBinaryFile(argv[1]);
    if(!network || parser->GetInputTensorNames().size()!=1) throw std::runtime_error("Expected one input");
    auto input=parser->GetNetworkInputBindingInfo(parser->GetInputTensorNames()[0]);
    powerscope::validate(input.second);
    auto names=parser->GetOutputTensorNames();
    if(names.empty())throw std::runtime_error("No outputs");
    std::vector<armnn::BindingPointInfo> bindings;
    for(const auto& name:names){bindings.push_back(parser->GetNetworkOutputBindingInfo(name));powerscope::validate(bindings.back().second);}
    std::ifstream samples(argv[2],std::ios::binary);
    if(!samples)throw std::runtime_error("Cannot read input");
    armnn::IRuntime::CreationOptions options;
    options.m_CustomAllocatorMap["CpuAcc"]=options.m_CustomAllocatorMap["CpuRef"]=std::make_shared<armnn::CmaAllocator>();
    auto runtime=armnn::IRuntime::Create(options);
    armnn::OptimizerOptionsOpaque optimizer;
    optimizer.SetImportEnabled(true);optimizer.SetExportEnabled(true);
    std::cout<<"stage=optimize backend="<<backend<<std::endl;
    auto optimized=armnn::Optimize(*network,{armnn::BackendId(backend)},runtime->GetDeviceSpec(),optimizer);
    if(!optimized)throw std::runtime_error("Selected backend optimization failed");
    std::ofstream graph(prefix+".dot");
    if(!graph || optimized->SerializeToDot(graph)!=armnn::Status::Success)throw std::runtime_error("Cannot write graph");
    armnn::NetworkId id;std::string error;
    armnn::INetworkProperties properties(false,armnn::MemorySource::Malloc,armnn::MemorySource::Malloc);
    std::cout<<"stage=load_network"<<std::endl;
    if(runtime->LoadNetwork(id,std::move(optimized),error,properties)!=armnn::Status::Success)throw std::runtime_error(error);
    auto profiler=runtime->GetProfiler(id);if(profiler)profiler->EnableProfiling(true);
    CmaBuffer in(input.second.GetNumBytes());input.second.SetConstant(true);
    armnn::InputTensors inputs{{input.first,armnn::ConstTensor(input.second,in.data.virt_addr)}};
    armnn::OutputTensors outputs;
    std::vector<std::unique_ptr<CmaBuffer>> buffers;
    std::vector<std::ofstream> files;
    std::ofstream metadata(prefix+".bindings.tsv"),timings(prefix+".csv");
    if(!metadata || !timings)throw std::runtime_error("Cannot write metadata");
    metadata<<"file_index\tname\tbinding_id\tdtype\telements\tscale\toffset\n";
    for(size_t i=0;i<names.size();++i){
        auto& binding=bindings[i];auto& info=binding.second;
        buffers.push_back(std::make_unique<CmaBuffer>(info.GetNumBytes()));
        outputs.emplace_back(binding.first,armnn::Tensor(info,buffers.back()->data.virt_addr));
        files.emplace_back(prefix+".out"+std::to_string(i)+".f32",std::ios::binary);
        if(!files.back())throw std::runtime_error("Cannot write logits");
        metadata<<i<<'\t'<<names[i]<<'\t'<<binding.first<<'\t'<<static_cast<int>(info.GetDataType())<<'\t'
                <<info.GetNumElements()<<'\t'<<info.GetQuantizationScale()<<'\t'<<info.GetQuantizationOffset()<<'\n';
    }
    timings<<"frame,requested_backend,inference_us\n";
    std::vector<float> values(input.second.GetNumElements());unsigned frames=0;
    while(samples.read(reinterpret_cast<char*>(values.data()),values.size()*sizeof(float))){
        powerscope::encode(values.data(),in.data.virt_addr,input.second);
        cma_mem_sync_for_device(in.data.phys_addr,in.data.size);
        const auto start=std::chrono::steady_clock::now();
        if(runtime->EnqueueWorkload(id,inputs,outputs)!=armnn::Status::Success)throw std::runtime_error("EnqueueWorkload failed");
        const auto us=std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::steady_clock::now()-start).count();
        timings<<frames<<','<<backend<<','<<us<<'\n';
        for(size_t i=0;i<bindings.size();++i){
            auto& buffer=buffers[i]->data;cma_mem_sync_for_cpu(buffer.phys_addr,buffer.size);
            std::vector<float> decoded(bindings[i].second.GetNumElements());
            powerscope::decode(buffer.virt_addr,decoded.data(),bindings[i].second);
            files[i].write(reinterpret_cast<const char*>(decoded.data()),decoded.size()*sizeof(float));
            if(!files[i])throw std::runtime_error("Logit write failed");
        }
        std::cout<<"frame="<<frames++<<" inference_us="<<us<<std::endl;
    }
    if(samples.gcount()!=0 || !samples.eof() || !frames)throw std::runtime_error("Empty or truncated input");
    if(!metadata || !timings)throw std::runtime_error("Metadata write failed");
    if(profiler){std::ofstream profile(prefix+".profile.json");profiler->Print(profile);}
    runtime->UnloadNetwork(id);
    std::cout<<"stage=complete frames="<<frames<<" requested_backend="<<backend<<"; inspect graph/profile"<<std::endl;
    return 0;
}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}
