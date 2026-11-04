// Persistent PS worker: raw A/B/C 500-point frames -> registered student -> 22 logits.
// IPC is PSF1 + uint32 little-endian sequence + 1500 little-endian float32.
// All neural operators use Alnpu. Statistical feature planes stay zero.
#include <armnn/ArmNN.hpp>
#include <armnn/IProfiler.hpp>
#include <armnnOnnxParser/IOnnxParser.hpp>
#include <armnnALUtils/cma_mem_init.hpp>
#include <array>
#include <chrono>
#include <cmath>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <memory>
#include <vector>
#include <fcntl.h>
#include <sys/file.h>
#include <unistd.h>
#include "tensor_io.hpp"

struct Buffer {
    cma_buffer_t b;
    explicit Buffer(unsigned n):b(n){b.flags=0;cma_mem_alloc(&b);if(!b.virt_addr||!b.phys_addr)throw std::runtime_error("CMA allocation failed");}
    ~Buffer(){if(b.phys_addr)cma_mem_free(&b);}
    Buffer(const Buffer&)=delete;Buffer& operator=(const Buffer&)=delete;
};
struct Model { armnn::NetworkId id; armnn::BindingPointInfo input,output; };
static Model load(armnn::IRuntime& rt,const char* path,const std::string& prefix){
    auto parser=armnnOnnxParser::IOnnxParser::Create();auto net=parser->CreateNetworkFromBinaryFile(path);
    if(!net||parser->GetInputTensorNames().size()!=1||parser->GetOutputTensorNames().size()!=1)throw std::runtime_error("Unexpected bindings");
    Model m;m.input=parser->GetNetworkInputBindingInfo(parser->GetInputTensorNames()[0]);m.output=parser->GetNetworkOutputBindingInfo(parser->GetOutputTensorNames()[0]);
    powerscope::validate(m.input.second);powerscope::validate(m.output.second);
    armnn::OptimizerOptionsOpaque opts;opts.SetImportEnabled(true);opts.SetExportEnabled(true);
    auto graph=armnn::Optimize(*net,{armnn::BackendId("Alnpu")},rt.GetDeviceSpec(),opts);
    if(!graph)throw std::runtime_error("Alnpu-only optimization failed");
    std::ofstream dot(prefix+".dot");if(!dot||graph->SerializeToDot(dot)!=armnn::Status::Success)throw std::runtime_error("Cannot save graph evidence");
    std::string err;armnn::INetworkProperties properties(false,armnn::MemorySource::Malloc,armnn::MemorySource::Malloc);
    if(rt.LoadNetwork(m.id,std::move(graph),err,properties)!=armnn::Status::Success)throw std::runtime_error(err);
    if(auto p=rt.GetProfiler(m.id))p->EnableProfiling(true);
    return m;
}
int main(int argc,char** argv)try{
    if(argc!=4&&argc!=5){std::cerr<<"Usage: ps_npu_worker TRUNK.onnx HEAD.onnx NEW_EVIDENCE_DIRECTORY [kd-s1|sfaug-r1]\n";return 2;}
    const std::string model=argc==5?argv[4]:"kd-s1";
    if(model!="kd-s1"&&model!="sfaug-r1")throw std::runtime_error("Unsupported model identity");
    int lock=open("/tmp/powerscope-dr1-npu.lock",O_CREAT|O_RDWR|O_NOFOLLOW,0600);
    if(lock<0||flock(lock,LOCK_EX|LOCK_NB)!=0)throw std::runtime_error("NPU worker already running or lock unavailable");
    if(!std::filesystem::create_directory(argv[3]))throw std::runtime_error("Evidence directory must be new");
    std::string prefix=std::string(argv[3])+"/";
    armnn::ConfigureLogging(false,false,armnn::LogSeverity::Error);
    armnn::IRuntime::CreationOptions opts;
    opts.m_CustomAllocatorMap["CpuRef"]=opts.m_CustomAllocatorMap["CpuAcc"]=std::make_shared<armnn::CmaAllocator>();
    auto rt=armnn::IRuntime::Create(opts);auto a=load(*rt,argv[1],prefix+"trunk"),b=load(*rt,argv[2],prefix+"head");
    const auto& ai=a.output.second;const auto& bi=b.input.second;
    if(ai.GetNumBytes()!=bi.GetNumBytes()||ai.GetNumElements()!=bi.GetNumElements()||ai.GetDataType()!=bi.GetDataType()||ai.GetQuantizationScale()!=bi.GetQuantizationScale()||ai.GetQuantizationOffset()!=bi.GetQuantizationOffset())throw std::runtime_error("Interstage mismatch");
    const auto& shape=a.input.second.GetShape();
    if(shape.GetNumDimensions()!=4||shape[0]!=1||shape[1]!=9||shape[2]!=32||shape[3]!=32||ai.GetShape()!=armnn::TensorShape({1,32,4,4})||bi.GetShape()!=armnn::TensorShape({1,512})||b.output.second.GetNumElements()!=22)throw std::runtime_error("Unexpected kd-s1 shape");
    Buffer input(a.input.second.GetNumBytes()),middle(ai.GetNumBytes()),output(b.output.second.GetNumBytes());
    a.input.second.SetConstant(true);b.input.second.SetConstant(true);
    armnn::InputTensors inA{{a.input.first,armnn::ConstTensor(a.input.second,input.b.virt_addr)}},inB{{b.input.first,armnn::ConstTensor(b.input.second,middle.b.virt_addr)}};
    armnn::OutputTensors outA{{a.output.first,armnn::Tensor(ai,middle.b.virt_addr)}},outB{{b.output.first,armnn::Tensor(b.output.second,output.b.virt_addr)}};
    std::array<float,1500> raw;std::array<float,9216> padded{};std::array<float,22> scores;
    std::vector<uint8_t> staging(a.input.second.GetNumBytes());unsigned count=0;bool saved=false;
    auto profiles=[&](){if(saved)return;for(auto p:std::vector<std::pair<armnn::NetworkId,std::string>>{{a.id,"trunk"},{b.id,"head"}}){auto prof=rt->GetProfiler(p.first);if(prof){prof->EnableProfiling(false);std::ofstream f(prefix+p.second+".profile.json");if(!f)throw std::runtime_error("Profile open failed");prof->Print(f);if(!f)throw std::runtime_error("Profile write failed");}}saved=true;};
    std::cout<<std::setprecision(9)<<"{\"type\":\"ready\",\"backend\":\"Alnpu\",\"model\":\""<<model<<"\",\"input_samples\":1500,\"output_classes\":22,\"statistics\":false}"<<std::endl;
    using Clock=std::chrono::steady_clock;auto us=[](auto dt){return std::chrono::duration_cast<std::chrono::microseconds>(dt).count();};
    while(true){
        std::array<unsigned char,8> header;std::cin.read(reinterpret_cast<char*>(header.data()),8);
        if(std::cin.gcount()==0&&std::cin.eof())break;
        if(std::cin.gcount()!=8||std::memcmp(header.data(),"PSF1",4)!=0)throw std::runtime_error("Invalid frame header");
        uint32_t seq=uint32_t(header[4])|(uint32_t(header[5])<<8)|(uint32_t(header[6])<<16)|(uint32_t(header[7])<<24);
        std::cin.read(reinterpret_cast<char*>(raw.data()),6000);if(std::cin.gcount()!=6000)throw std::runtime_error("Truncated waveform");
        auto begin=Clock::now();
        for(float x:raw)if(!std::isfinite(x)||std::abs(x)>1.00001f)throw std::runtime_error("Waveform must be finite, normalized to [-1,1]");
        for(unsigned c=0;c<3;++c)std::copy_n(raw.begin()+c*500,500,padded.begin()+c*1024);
        powerscope::encode(padded.data(),staging.data(),a.input.second);std::memcpy(input.b.virt_addr,staging.data(),staging.size());cma_mem_sync_for_device(input.b.phys_addr,input.b.size);
        auto t0=Clock::now();if(rt->EnqueueWorkload(a.id,inA,outA)!=armnn::Status::Success)throw std::runtime_error("Trunk execution failed");auto t1=Clock::now();
        cma_mem_sync_for_device(middle.b.phys_addr,middle.b.size);
        if(rt->EnqueueWorkload(b.id,inB,outB)!=armnn::Status::Success)throw std::runtime_error("Head execution failed");auto t2=Clock::now();
        cma_mem_sync_for_cpu(output.b.phys_addr,output.b.size);powerscope::decode(output.b.virt_addr,scores.data(),b.output.second);auto end=Clock::now();
        std::cout<<"{\"type\":\"result\",\"backend\":\"Alnpu\",\"sequence\":"<<seq<<",\"prepare_us\":"<<us(t0-begin)<<",\"trunk_us\":"<<us(t1-t0)<<",\"head_us\":"<<us(t2-t1)<<",\"inference_us\":"<<us(t2-t0)<<",\"worker_us\":"<<us(end-begin)<<",\"logits\":[";
        for(unsigned j=0;j<22;++j)std::cout<<(j?",":"")<<scores[j];std::cout<<"]}"<<std::endl;
        if(++count==16)profiles();
    }
    if(count)profiles();rt->UnloadNetwork(a.id);rt->UnloadNetwork(b.id);close(lock);return 0;
}catch(const std::exception& e){std::cerr<<e.what()<<std::endl;return 1;}
