#pragma once
#include <armnn/Tensor.hpp>
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <limits>
#include <stdexcept>

namespace powerscope {
inline void validate(const armnn::TensorInfo& info) {
    const auto type=info.GetDataType();
    if(type==armnn::DataType::Float32) return;
    if(type!=armnn::DataType::QAsymmU8 && type!=armnn::DataType::QAsymmS8 && type!=armnn::DataType::QSymmS8)
        throw std::runtime_error("Unsupported SDK binding dtype");
    const auto& scales=info.GetQuantizationScales();
    if(scales.size()!=1 || !std::isfinite(scales[0]) || scales[0]<=0)
        throw std::runtime_error("Expected finite positive per-tensor quantization scale");
    const auto z=info.GetQuantizationOffset();
    if((type==armnn::DataType::QAsymmU8 && (z<0 || z>255)) ||
       (type!=armnn::DataType::QAsymmU8 && (z< -128 || z>127)) ||
       (type==armnn::DataType::QSymmS8 && z!=0))
        throw std::runtime_error("Invalid quantization zero point");
}
// ONNX QuantizeLinear round-to-nearest, ties-to-even; independent of fenv.
inline double nearestEven(double x) {
    double lo=std::floor(x), fraction=x-lo;
    return lo+(fraction>0.5 || (fraction==0.5 && std::fmod(lo,2.0)!=0.0));
}
template<class T> inline void quantize(const float* values, T* dest, const armnn::TensorInfo& info) {
    for(unsigned i=0;i<info.GetNumElements();++i) {
        double q=nearestEven(static_cast<double>(values[i])/info.GetQuantizationScale())+info.GetQuantizationOffset();
        dest[i]=static_cast<T>(std::clamp(q,static_cast<double>(std::numeric_limits<T>::min()),static_cast<double>(std::numeric_limits<T>::max())));
    }
}
inline void encode(const float* values, void* dest, const armnn::TensorInfo& info) {
    validate(info);
    for(unsigned i=0;i<info.GetNumElements();++i)
        if(!std::isfinite(values[i])) throw std::runtime_error("Non-finite input");
    if(info.GetDataType()==armnn::DataType::Float32) std::memcpy(dest,values,info.GetNumBytes());
    else if(info.GetDataType()==armnn::DataType::QAsymmU8) quantize(values,static_cast<uint8_t*>(dest),info);
    else quantize(values,static_cast<int8_t*>(dest),info);
}
inline void decode(const void* source, float* values, const armnn::TensorInfo& info) {
    validate(info);
    for(unsigned i=0;i<info.GetNumElements();++i) {
        if(info.GetDataType()==armnn::DataType::Float32) values[i]=static_cast<const float*>(source)[i];
        else {
            int q=info.GetDataType()==armnn::DataType::QAsymmU8?static_cast<const uint8_t*>(source)[i]:static_cast<const int8_t*>(source)[i];
            values[i]=(q-info.GetQuantizationOffset())*info.GetQuantizationScale();
        }
        if(!std::isfinite(values[i])) throw std::runtime_error("Non-finite output");
    }
}
}
