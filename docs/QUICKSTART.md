# 运行与完整演示

## 1. 电脑端准备

安装 Python 3.10 或更新版本，在仓库根目录运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pc_monitor --port 18768 --demo-assets assets/pc-demo-manifest.json
```

浏览器打开 `http://127.0.0.1:18768/`。串口监视器选择实际 USB-RS485 的 COM 口；协议参数为 **115200 bit/s、8 数据位、偶校验、1 停止位（8E1），从站地址 1**。连接 A/B 对应板端已核对的 RS485 A0/B0，不使用 CAN 的 L0/H0。

`assets/pc-demo-manifest.json` 引用每类一窗的演示样本，供样本发送模式使用。在线 Simulink 模式持续产生新的窗口。

## 2. 构建 Simulink 在线模型

环境：Windows、MATLAB R2023b、Simulink、Simscape Electrical，已配置 MinGW C/C++ MEX 编译器。原始电路源文件在 `simulation/source/`；构建会复制源模型，生成 `powerscope_npc_live.slx` 和本机 MEX 桥。

在 MATLAB 中执行（修改两个绝对路径）：

```matlab
projectRoot = 'D:/work/powerscope-dr1-open';
runtimeRoot = 'D:/work/powerscope-runtime';
addpath(fullfile(projectRoot, 'matlab'));
powerscope_build(projectRoot, runtimeRoot, fullfile(projectRoot, 'simulation', 'source'));
```

`runtimeRoot` 建议使用英文路径，与源码目录分开。构建结果与源码哈希写入 `build.json`，接收器会检查其一致性。

停止第一步的 Python 监视器（Ctrl+C），随后以 Simulink 接收模式启动：

```powershell
.\.venv\Scripts\python.exe -m pc_monitor --port 18768 --simulation-engine simulink --simulink-runtime D:/work/powerscope-runtime --demo-assets assets/pc-demo-manifest.json
```

在 MATLAB 中打开控制台：

```matlab
powerscope_demo(projectRoot, runtimeRoot, 18768);
```

原生控制台提供三相电流、线电压/功率信息、故障注入和恢复；Simulink 的 `Current_Scope` 可同时查看测量电流与真实电流。具体控件说明见 [Simulink 演示](SIMULINK_DEMO.md)。

## 3. 连接已部署的 DR1

板端需运行与硬件设计匹配的 UIO 配置、Alnpu worker 和 PowerScope PS 服务，监听板端 `127.0.0.1:8765`。用已有板端 SSH 账户建立转发，替换账户及 IP：

```sh
ssh -N -L 127.0.0.1:18765:127.0.0.1:8765 BOARD_USER@BOARD_IP
```

将 HDMI 屏连接板端 HDMI、USB 触摸连接板端 USB；启用对应的板端显示服务。硬件镜像、设备树、UIO 与屏幕驱动需作为配套版本使用，详见 [构建说明](BUILD.md)。

打开监视器并连接 USB-RS485。启动 Simulink 连续运行后，使用控制台的板端连接功能，把产生的三相电流窗口经网口送到 DR1。输入请求只含样本、序号、哈希及显示尺度，故障参考标签不发给分类器。

## 4. 录制顺序

1. 拍摄电脑、DR1、HDMI 屏、USB-RS485 的全景，说明信号与诊断链路。
2. 保持正常运行：拍摄 Simulink 的三相正弦电流、板端正常诊断和递增帧号。
3. 注入 **Ta2 开关开路**：拍摄电流波形变化，以及板端类别与置信度更新。
4. 拍摄电脑 **RS485 接收报文**：展示原始 HEX、CRC、故障类别、序号和刷新计数。
5. 点击恢复正常；再选择一个传感器故障，展示波形与诊断同步变化。
6. 恢复正常并结束。板端 NPU 耗时与电脑接收延迟是两个不同指标。

演示从系统已运行且各界面可见的状态开始，采用当前屏幕已初始化的演示配置。配套视频下载见项目 Release。
