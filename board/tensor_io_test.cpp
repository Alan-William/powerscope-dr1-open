#include "tensor_io.hpp"
#include <iostream>
#include <vector>

static void require(bool ok) { if(!ok) throw std::runtime_error("tensor IO check failed"); }
int main() try {
    armnn::TensorInfo u({7},armnn::DataType::QAsymmU8,0.5f,128);
    float x[]={-1000,-1.25f,-0.75f,0.25f,0.75f,1.25f,1000};
    uint8_t bytes[7]; float y[7];
    powerscope::encode(x,bytes,u);
    const uint8_t expected[]={0,126,126,128,130,130,255};
    for(int i=0;i<7;++i) require(bytes[i]==expected[i]);
    powerscope::decode(bytes,y,u);
    for(int i=0;i<7;++i) require(y[i]==(int(expected[i])-128)*0.5f);
    armnn::TensorInfo s({7},armnn::DataType::QAsymmS8,0.5f,-1);
    int8_t signedBytes[7]; powerscope::encode(x,signedBytes,s);
    const int8_t expectedSigned[]={-128,-3,-3,-1,1,1,127};
    for(int i=0;i<7;++i) require(signedBytes[i]==expectedSigned[i]);
    armnn::TensorInfo f({7},armnn::DataType::Float32);
    powerscope::encode(x,y,f);for(int i=0;i<7;++i) require(x[i]==y[i]);
    auto rejects=[&](auto action){bool rejected=false;try{action();}catch(const std::runtime_error&){rejected=true;}require(rejected);};
    rejects([&]{armnn::TensorInfo bad({1},armnn::DataType::QAsymmU8,0.0f,0);powerscope::validate(bad);});
    rejects([&]{armnn::TensorInfo bad({1},armnn::DataType::Signed32);powerscope::validate(bad);});
    rejects([&]{x[0]=std::numeric_limits<float>::quiet_NaN();powerscope::encode(x,bytes,u);});
    std::cout<<"PASS quantized IO: ties-even, saturation, signed, float, invalid scale/dtype/NaN\n";
    return 0;
} catch(const std::exception& e){std::cerr<<e.what()<<"\n";return 1;}
