// Offline-prepared probe for the vendor Arm NN SDK. Never enables CPU fallback.
#include <armnn/ArmNN.hpp>
#include <armnn/IProfiler.hpp>
#include <armnnOnnxParser/IOnnxParser.hpp>
#include <armnnALUtils/cma_mem_init.hpp>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstring>
#include <fstream>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <vector>
#include "tensor_io.hpp"

struct Buffer {
    cma_buffer_t data;
    explicit Buffer(unsigned bytes):data(bytes) {
        data.flags=0;
        cma_mem_alloc(&data);
        if (!data.virt_addr || !data.phys_addr) throw std::runtime_error("CMA allocation failed");
    }
    ~Buffer(){ if(data.phys_addr) cma_mem_free(&data); }
    Buffer(const Buffer&)=delete;
    Buffer& operator=(const Buffer&)=delete;
};

int main(int argc,char** argv) try {
    if(argc!=4) {
        std::cerr<<"Usage: npu_probe MODEL.onnx golden_f32.bin OUTPUT_PREFIX\n";
        return 2;
    }
    const std::string prefix=argv[3];
    auto parser=armnnOnnxParser::IOnnxParser::Create();
    auto network=parser->CreateNetworkFromBinaryFile(argv[1]);
    if(parser->GetInputTensorNames().size()!=1 || parser->GetOutputTensorNames().size()!=1)
        throw std::runtime_error("Expected one input and one output");
    auto input=parser->GetNetworkInputBindingInfo(parser->GetInputTensorNames()[0]);
    auto output=parser->GetNetworkOutputBindingInfo(parser->GetOutputTensorNames()[0]);
    if(input.second.GetNumElements()!=1500 || output.second.GetNumElements()!=22)
        throw std::runtime_error("Expected 1500-element input and 22 logits");
    powerscope::validate(input.second); powerscope::validate(output.second);
    std::cout<<"SDK binding input dtype="<<static_cast<int>(input.second.GetDataType())
             <<" scale="<<input.second.GetQuantizationScale()<<" offset="<<input.second.GetQuantizationOffset()
             <<" output dtype="<<static_cast<int>(output.second.GetDataType())
             <<" scale="<<output.second.GetQuantizationScale()<<" offset="<<output.second.GetQuantizationOffset()<<"\n";
    armnn::IRuntime::CreationOptions options;
    options.m_CustomAllocatorMap["CpuAcc"]=options.m_CustomAllocatorMap["CpuRef"]=
        std::make_shared<armnn::CmaAllocator>();
    auto runtime=armnn::IRuntime::Create(options);
    armnn::OptimizerOptionsOpaque optimizer;
    optimizer.SetImportEnabled(true); optimizer.SetExportEnabled(true);
    auto optimized=armnn::Optimize(*network,{armnn::BackendId("Alnpu")},runtime->GetDeviceSpec(),optimizer);
    if(!optimized) throw std::runtime_error("Alnpu-only optimization failed; CPU fallback is disabled");
    std::ofstream graph(prefix+".dot");
    if(!graph || optimized->SerializeToDot(graph)!=armnn::Status::Success)
        throw std::runtime_error("Cannot write optimized graph");
    armnn::NetworkId id;
    std::string error;
    armnn::INetworkProperties properties(false,armnn::MemorySource::Malloc,armnn::MemorySource::Malloc);
    if(runtime->LoadNetwork(id,std::move(optimized),error,properties)!=armnn::Status::Success)
        throw std::runtime_error("LoadNetwork: "+error);
    auto profiler=runtime->GetProfiler(id);
    if(profiler) profiler->EnableProfiling(true);
    Buffer in(input.second.GetNumBytes()),out(output.second.GetNumBytes());
    input.second.SetConstant(true);
    armnn::InputTensors inputs{{input.first,armnn::ConstTensor(input.second,in.data.virt_addr)}};
    armnn::OutputTensors outputs{{output.first,armnn::Tensor(output.second,out.data.virt_addr)}};
    std::ifstream samples(argv[2],std::ios::binary);
    std::ofstream logits(prefix+".logits.bin",std::ios::binary),timings(prefix+".csv");
    if(!samples||!logits||!timings) throw std::runtime_error("Cannot open input/output files");
    timings<<"frame,requested_backend,inference_us,label\n";
    unsigned frame=0;
    std::vector<float> inputValues(1500),values(22);
    while(samples.read(reinterpret_cast<char*>(inputValues.data()),inputValues.size()*sizeof(float))) {
        powerscope::encode(inputValues.data(),in.data.virt_addr,input.second);
        cma_mem_sync_for_device(in.data.phys_addr,in.data.size);
        auto begin=std::chrono::steady_clock::now();
        if(runtime->EnqueueWorkload(id,inputs,outputs)!=armnn::Status::Success)
            throw std::runtime_error("EnqueueWorkload failed");
        auto us=std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::steady_clock::now()-begin).count();
        cma_mem_sync_for_cpu(out.data.phys_addr,out.data.size);
        powerscope::decode(out.data.virt_addr,values.data(),output.second);
        timings<<frame++<<",Alnpu,"<<us<<","<<(std::max_element(values.begin(),values.end())-values.begin())<<"\n";
        logits.write(reinterpret_cast<const char*>(values.data()),values.size()*sizeof(float));
    }
    if(samples.gcount()!=0 || !samples.eof() || frame==0) throw std::runtime_error("Empty/truncated input");
    if(!logits||!timings) throw std::runtime_error("Output write failed");
    if(profiler) {std::ofstream profile(prefix+".profile.json"); profiler->Print(profile);}
    runtime->UnloadNetwork(id);
    std::cout<<"Completed "<<frame<<" frames with Alnpu-only request. Inspect graph/profile and compare logits.\n";
    return 0;
} catch(const std::exception& e) {std::cerr<<e.what()<<"\n";return 1;}
