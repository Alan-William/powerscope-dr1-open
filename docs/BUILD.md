# FPGA、板端程序与本机验证

## FPGA 集成

本仓库提供完整原创外设 RTL 和集成脚本。准备与 DR1 硬件一致的厂商 NPU 演示 `demo.zip` 及安路 TD 工具，参数使用自己的安装位置：

```powershell
python tools/build_pl_soc.py --vendor-zip D:/vendor/demo.zip --td-dir D:/Anlogic/TD_Release_2026.1_SP2.200.067 --work-root D:/fpga-work --clock-plan shared-axi --profile display --timing-effort high --stage-only
```

`--stage-only` 生成独立工程。移除此参数执行综合和布局布线。`display` 保留 NPU 与 HDMI；`headless` 用于 NPU/AXI/RS485；`full` 为原厂全功能集成对照。外设使用 75 MHz、NPU 使用 150 MHz。最终时序结果以该次编译报告为准。

地址分配和 UIO 合同见 [hardware-contract.md](hardware-contract.md)。`configs/*.pl-candidate.json` 是模板，需要根据实际镜像、sysfs、map0 和设备节点生成验证配置。启动镜像、设备树和内核视频驱动必须配套；部署工具不会使不同历史镜像自动兼容。`board/deploy/` 中恢复工具保留了提交系统的版本约束，移植时应先检查具体脚本。

## 板端 C++

`board/CMakeLists.txt` 构建持久化 NPU worker、MMIO 库和诊断探针，依赖与板端一致的 Arm NN/Alnpu SDK。交叉编译使用 `board/toolchains/aarch64-sdk.cmake`，设置 `POWERSCOPE_CROSS_ROOT`、`POWERSCOPE_BOARD_SYSROOT`、`POWERSCOPE_CRT_DIR`：

```sh
cmake -S board -B build/board -DCMAKE_TOOLCHAIN_FILE="$PWD/board/toolchains/aarch64-sdk.cmake" -DANLOGIC_ARMNN_ROOT=/path/to/armnn_lib
cmake --build build/board --parallel
```

将 worker/MMIO 库放入板端发布目录的 `bin/`，将本仓库 `assets/`、`ps/`、`protocol/`、`board/` 同步到该目录。通过 `POWERSCOPE_VENDOR_LIB` 指定板端 Arm NN 运行库位置，通过 `POWERSCOPE_STORE` 指定日志目录。`run_ps_replay.sh` 展示程序启动参数，`run_production.sh` 展示纯 NPU + PL Q15 的正式链路。

板端 HDMI 程序位于 `board_display/`；使用板端匹配的 Qt/PyQt5 环境和帧缓冲/触摸设备。启动与显示数据接口见 [HDMI_TOUCH_DEMO.md](HDMI_TOUCH_DEMO.md)。

## 软件与协议测试

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

测试使用本机模拟对象与固定向量；Linux 运行 PS 服务合同测试；Windows 自动跳过依赖 fcntl 的 PS 测试组。未编译本机可选电路求解器时，其 8 项测试按依赖条件跳过。它们检查协议、网络样本约束、流控、错误恢复、显示数据及 RS485 关联，不会连接真实板卡。

安装 Icarus Verilog 后，使用英文临时目录运行 RTL 仿真：

```sh
python tools/run_pl_tests.py --iverilog /path/to/iverilog --work-root /tmp/powerscope-rtl
```

Windows 可将路径替换为 `D:/tools/iverilog/bin/iverilog.exe` 与 `D:/fpga-work`。测试覆盖邮箱、原子应答、UART/RTU、地址路由与样本回读。仿真结果与板上 NPU 验收记录分别保存。
