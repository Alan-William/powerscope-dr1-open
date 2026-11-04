# FPGA PL 数据通路

原创 Verilog 模块包含 AXI-Lite 路由/寄存器、样本 RAM、原子结果邮箱、UART、Modbus RTU、CRC 与 RS485 方向控制。样本通道为 758 个 32 位字，包含帧头及三相 1500 点 Q15 数据；结果通道采用提交、确认、消费与有效期管理。

硬件地址及接口见 [硬件合同](../docs/hardware-contract.md) 和 [接口定义](../docs/INTERFACES.md)。集成、构建与 HDL 验证命令见 [构建说明](../docs/BUILD.md)。

`integration/` 提供 headless、display 和通用 UIO 设备树片段。与本机真实硬件对应的地址和设备节点由部署时的 sysfs 检查确定。
