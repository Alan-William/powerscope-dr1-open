# 第三方依赖与研究来源

本仓库的原创 RTL、应用程序、模型训练代码、Simulink 工程配置、发布的模型参数及随附文档采用根目录 MIT 许可证。已有的版权声明应保留；引用的论文、厂商商标和外部软件不因此改变其许可。

| 外部组件 | 使用位置 | 获取方式 |
| --- | --- | --- |
| 安路 TD、飞龙 SoC/NPU 工程、Alnpu 后端 | FPGA 集成与板端推理 | 从安路/板卡厂商获得对应版本，遵守其许可 |
| Arm NN、ONNX parser、protobuf | `board/` C++ 程序链接依赖 | 使用与板端镜像匹配的厂商 SDK |
| MATLAB、Simulink、Simscape Electrical、MinGW 支持包 | `simulation/source/` 电力电路模型与 MEX 桥 | 通过 MathWorks 安装并授权 |
| Python、NumPy、pyserial、PyTorch、ONNX | 工具、训练、推理模型导出 | 分别通过官方分发渠道安装 |
| Qt / PyQt5 | DR1 HDMI 触摸显示程序 | 使用板卡系统提供的 Qt/PyQt5 环境，遵守其许可 |
| Icarus Verilog | RTL 仿真测试 | 单独安装开源 HDL 仿真器 |

厂商 SDK、厂商 HDL/IP、工具链、Linux 镜像与生成位流不随本仓库再分发。`tools/build_pl_soc.py` 在用户提供的原厂工程副本上进行集成，第三方文件仍适用原有条款。

研究基础：Xu 等，*Open-Switch and Current Sensor Faults Diagnosis in Three-Level NPC Inverters Under Small Samples*，2026。本项目的三电平 NPC 建模及故障任务参考实验室相关研究；FPGA 数据通路、SoC 集成、轻量网络部署和交互演示的实现见本仓库源码。论文正文不随仓库分发。

`assets/demo22.f32` 是每类一窗的仿真演示样本；完整训练集、验证集和测试集单独管理。该小样本文件用于运行演示和检查数据格式，不用于重新计算报告中的完整数据集准确率。
