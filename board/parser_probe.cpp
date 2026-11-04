// Calls the actual vendor parser; parse mode never creates an Arm NN runtime.
#include <armnn/ArmNN.hpp>
#include <armnnOnnxParser/IOnnxParser.hpp>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>

static void printBinding(const char* direction, const std::string& name,
                         const armnn::BindingPointInfo& binding) {
    std::cout << direction << " name=" << name << " id=" << binding.first
              << " dtype=" << static_cast<int>(binding.second.GetDataType()) << " shape=";
    const auto& shape=binding.second.GetShape();
    for (unsigned i=0;i<shape.GetNumDimensions();++i) std::cout << (i?",":"") << shape[i];
    std::cout << " bytes=" << binding.second.GetNumBytes()
              << " scale=" << binding.second.GetQuantizationScale()
              << " offset=" << binding.second.GetQuantizationOffset() << std::endl;
}

int main(int argc,char** argv) try {
    if(argc<2 || argc>4) {
        std::cerr << "Usage: parser_probe MODEL.onnx [parse|optimize-alnpu] [GRAPH.dot]\n";
        return 2;
    }
    const std::string mode=argc>2?argv[2]:"parse";
    if(mode!="parse" && mode!="optimize-alnpu") throw std::runtime_error("Unknown mode");
    armnn::ConfigureLogging(true,false,armnn::LogSeverity::Debug);
    std::cout << "stage=parser_create" << std::endl;
    auto parser=armnnOnnxParser::IOnnxParser::Create();
    std::cout << "stage=parse_begin" << std::endl;
    auto network=parser->CreateNetworkFromBinaryFile(argv[1]);
    if(!network) throw std::runtime_error("Parser returned null network");
    for(const auto& name:parser->GetInputTensorNames())
        printBinding("input",name,parser->GetNetworkInputBindingInfo(name));
    for(const auto& name:parser->GetOutputTensorNames())
        printBinding("output",name,parser->GetNetworkOutputBindingInfo(name));
    std::cout << "stage=parse_ok" << std::endl;
    if(mode=="parse") return 0;
    std::cout << "stage=runtime_create" << std::endl;
    auto runtime=armnn::IRuntime::Create(armnn::IRuntime::CreationOptions());
    std::cout << "stage=optimize_alnpu_begin" << std::endl;
    auto optimized=armnn::Optimize(*network,{armnn::BackendId("Alnpu")},runtime->GetDeviceSpec());
    if(!optimized) throw std::runtime_error("Alnpu-only optimization returned null");
    if(argc==4) {
        std::ofstream out(argv[3]);
        if(!out || optimized->SerializeToDot(out)!=armnn::Status::Success)
            throw std::runtime_error("Cannot serialize optimized graph");
    }
    std::cout << "stage=optimize_alnpu_ok (no workload loaded or executed)" << std::endl;
    return 0;
} catch(const std::exception& e) {
    std::cerr << "stage=failed error=" << e.what() << std::endl;
    return 1;
}
