// Every neural operator executes on the explicitly selected backend.
// The interstage Flatten is only a tensor view over the same CMA allocation.
#include <armnn/ArmNN.hpp>
#include <armnn/IProfiler.hpp>
#include <armnnOnnxParser/IOnnxParser.hpp>
#include <armnnALUtils/cma_mem_init.hpp>
#include <chrono>
#include <cstring>
#include <fstream>
#include <filesystem>
#include <iostream>
#include <memory>
#include <vector>
#include "tensor_io.hpp"

struct Buffer {
    cma_buffer_t b;
    explicit Buffer(unsigned n):b(n){b.flags=0;cma_mem_alloc(&b);if(!b.virt_addr||!b.phys_addr)throw std::runtime_error("CMA allocation failed");}
    ~Buffer(){if(b.phys_addr)cma_mem_free(&b);}
    Buffer(const Buffer&)=delete;Buffer& operator=(const Buffer&)=delete;
};
struct Model {
    armnn::NetworkId id;
    armnn::BindingPointInfo input,output;
};
static Model load(armnn::IRuntime& runtime,const char* path,const std::string& backend,const std::string& prefix){
    auto parser=armnnOnnxParser::IOnnxParser::Create();auto net=parser->CreateNetworkFromBinaryFile(path);
    if(!net||parser->GetInputTensorNames().size()!=1||parser->GetOutputTensorNames().size()!=1)throw std::runtime_error("Expected one input/output");
    Model m;m.input=parser->GetNetworkInputBindingInfo(parser->GetInputTensorNames()[0]);m.output=parser->GetNetworkOutputBindingInfo(parser->GetOutputTensorNames()[0]);
    powerscope::validate(m.input.second);powerscope::validate(m.output.second);
    armnn::OptimizerOptionsOpaque options;options.SetImportEnabled(true);options.SetExportEnabled(true);
    auto opt=armnn::Optimize(*net,{armnn::BackendId(backend)},runtime.GetDeviceSpec(),options);
    if(!opt)throw std::runtime_error("Explicit backend optimization failed");
    std::ofstream dot(prefix+".dot");if(!dot||opt->SerializeToDot(dot)!=armnn::Status::Success)throw std::runtime_error("Graph write failed");
    std::string err;armnn::INetworkProperties props(false,armnn::MemorySource::Malloc,armnn::MemorySource::Malloc);
    if(runtime.LoadNetwork(m.id,std::move(opt),err,props)!=armnn::Status::Success)throw std::runtime_error(err);
    auto p=runtime.GetProfiler(m.id);if(p)p->EnableProfiling(true);
    return m;
}
int main(int argc,char** argv)try{
    if(argc<5||argc>6){std::cerr<<"Usage: student_pipeline_probe TRUNK HEAD INPUT_F32 OUTPUT_PREFIX [Alnpu|ALRef|CpuRef]\n";return 2;}
    const std::string backend=argc==6?argv[5]:"Alnpu",prefix=argv[4];
    if(std::filesystem::weakly_canonical(argv[3])==std::filesystem::weakly_canonical(prefix+".f32"))
        throw std::runtime_error("Output would overwrite input; choose a distinct prefix");
    if(backend!="Alnpu"&&backend!="ALRef"&&backend!="CpuRef")throw std::runtime_error("Unknown backend");
    armnn::ConfigureLogging(true,false,armnn::LogSeverity::Error);
    std::cout<<"requested_backend="<<backend<<" fallback=disabled interstage=shared_cma_view"<<std::endl;
    armnn::IRuntime::CreationOptions opts;opts.m_CustomAllocatorMap["CpuRef"]=opts.m_CustomAllocatorMap["CpuAcc"]=std::make_shared<armnn::CmaAllocator>();
    auto rt=armnn::IRuntime::Create(opts);auto a=load(*rt,argv[1],backend,prefix+".trunk"),b=load(*rt,argv[2],backend,prefix+".head");
    const auto& ai=a.output.second;const auto& bi=b.input.second;
    if(ai.GetNumElements()!=bi.GetNumElements()||ai.GetNumBytes()!=bi.GetNumBytes()||ai.GetDataType()!=bi.GetDataType()||ai.GetQuantizationScale()!=bi.GetQuantizationScale()||ai.GetQuantizationOffset()!=bi.GetQuantizationOffset())throw std::runtime_error("Interstage tensor mismatch");
    if(ai.GetShape().GetNumDimensions()!=4||ai.GetShape()[0]!=1||bi.GetShape().GetNumDimensions()!=2||bi.GetShape()[0]!=1||b.output.second.GetNumElements()!=22)throw std::runtime_error("Unexpected stage shapes");
    Buffer input(a.input.second.GetNumBytes()),middle(ai.GetNumBytes()),output(b.output.second.GetNumBytes());
    a.input.second.SetConstant(true);b.input.second.SetConstant(true);
    armnn::InputTensors inA{{a.input.first,armnn::ConstTensor(a.input.second,input.b.virt_addr)}},inB{{b.input.first,armnn::ConstTensor(b.input.second,middle.b.virt_addr)}};
    armnn::OutputTensors outA{{a.output.first,armnn::Tensor(a.output.second,middle.b.virt_addr)}},outB{{b.output.first,armnn::Tensor(b.output.second,output.b.virt_addr)}};
    std::ifstream samples(argv[3],std::ios::binary);std::ofstream logits(prefix+".f32",std::ios::binary),timing(prefix+".csv"),meta(prefix+".bindings.tsv");
    if(!samples||!logits||!timing||!meta)throw std::runtime_error("Cannot open files");
    meta<<"stage\telements\tscale\tzero_point\n";
    for(const auto& t:std::vector<std::pair<const char*,armnn::TensorInfo>>{{"input",a.input.second},{"middle",ai},{"output",b.output.second}})meta<<t.first<<'\t'<<t.second.GetNumElements()<<'\t'<<t.second.GetQuantizationScale()<<'\t'<<t.second.GetQuantizationOffset()<<'\n';
    timing<<"frame,backend,input_encode_copy_us,trunk_us,head_us,total_us,output_decode_us,frame_us\n";
    std::vector<float> x(a.input.second.GetNumElements()),y(22);
    // Quantize in cached host memory; one bulk DMA-buffer copy avoids thousands
    // of slow byte writes to uncached CMA pages for every input frame.
    std::vector<uint8_t> staging(a.input.second.GetNumBytes());unsigned n=0;
    using Clock=std::chrono::steady_clock;
    auto us=[](auto d){return std::chrono::duration_cast<std::chrono::microseconds>(d).count();};
    while(samples.read(reinterpret_cast<char*>(x.data()),x.size()*sizeof(float))){
        // Vendor profiler serialization becomes very slow for long histories.
        // Keep execution evidence for 16 frames; still check every output.
        if(n==16){for(auto id:{a.id,b.id}){auto p=rt->GetProfiler(id);if(p)p->EnableProfiling(false);}}
        auto tBegin=Clock::now();powerscope::encode(x.data(),staging.data(),a.input.second);
        std::memcpy(input.b.virt_addr,staging.data(),staging.size());cma_mem_sync_for_device(input.b.phys_addr,input.b.size);
        auto t0=Clock::now();if(rt->EnqueueWorkload(a.id,inA,outA)!=armnn::Status::Success)throw std::runtime_error("Trunk failed");auto t1=Clock::now();
        // The exporter may copy its DMA output via the CPU. Make both memory
        // paths coherent without reading, transforming, or copying activations.
        cma_mem_sync_for_device(middle.b.phys_addr,middle.b.size);
        if(rt->EnqueueWorkload(b.id,inB,outB)!=armnn::Status::Success)throw std::runtime_error("Head failed");auto t2=Clock::now();
        cma_mem_sync_for_cpu(output.b.phys_addr,output.b.size);powerscope::decode(output.b.virt_addr,y.data(),b.output.second);auto tEnd=Clock::now();
        logits.write(reinterpret_cast<const char*>(y.data()),y.size()*sizeof(float));timing<<n++<<','<<backend<<','<<us(t0-tBegin)<<','<<us(t1-t0)<<','<<us(t2-t1)<<','<<us(t2-t0)<<','<<us(tEnd-t2)<<','<<us(tEnd-tBegin)<<'\n';
    }
    if(samples.gcount()!=0||!samples.eof()||!n||!logits||!timing||!meta)throw std::runtime_error("Invalid input or output");
    logits.flush();timing.flush();meta.flush();
    if(!logits||!timing||!meta)throw std::runtime_error("Output flush failed");
    std::cout<<"inference_complete frames="<<n<<" profiling_first_frames=16"<<std::endl;
    for(auto p:std::vector<std::pair<armnn::NetworkId,const char*>>{{a.id,"trunk"},{b.id,"head"}}){auto profiler=rt->GetProfiler(p.first);if(profiler){std::ofstream f(prefix+"."+p.second+".profile.json");profiler->Print(f);}rt->UnloadNetwork(p.first);}
    std::cout<<"complete frames="<<n<<" backend="<<backend<<" all_neural_ops_on_requested_backend"<<std::endl;return 0;
}catch(const std::exception& e){std::cerr<<e.what()<<std::endl;return 1;}
