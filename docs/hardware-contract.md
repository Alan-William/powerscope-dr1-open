# PowerScope 硬件合同：PL 接手候选

2026-11-01，北京时间。RTL 基线 `236df82`，开发分支 `codex/pl-axi-rs485-20261101`。

这是**已部署的 NPU / 网口 / AXI / RS485 精简 SoC**，RTL 基线 `236df82`。150 MHz 布线 setup/hold 为 +0.001/+0.020 ns；实际启动、UIO、NPU 回归、样本往返与 RS485 短测均已有证据，联合长测状态见 [部署验收](VALIDATION.md)。下列地址已经与 live DT、平台设备及 UIO map 核对。仓库候选配置仍为 `verified_design=false`、`device=null`，另生成的板端 verified 配置才允许访问。

## 工程身份与共存

| 项目 | 证据 / 实际值 |
|---|---|
| 板卡 / 器件 | MLK-F3P-CZ02_DR1 / DR1M90GEG400，speed -2 |
| 厂商输入 | 本机 `05-5_NPU演示/demo.zip` 中 `soc_prj/soc_prj.al`；归档 SHA256 写入每轮 `integration.json` |
| 顶层 | `system_top`；默认 headless，保留 PS/NPU、RS485、状态灯与原 I2C 引脚，摄像头保持复位且停钟 |
| 工具 | TD Release_2026.1_SP2，build 6.2.2.200067 |
| 复建入口 | `tools/build_pl_soc.py`，每次建立独立 ASCII 目录并保留源文件/脚本哈希 |
| 接入位置 | GP0 → 原厂 `AXI_Protocol_Converter_0` → `ps_axi_lite_router`；m1 接本项目外设，默认 m0 接 `ps_axi_lite_error` |
| 原厂视频地址 | headless 下 0x80100000..0x80101FFF、0x80200000..0x80200FFF、0x80300000..0x8030FFFF 等非本项目 GP0 地址返回 DECERR；仅对照 `--profile full` 保留原矩阵和视频外设 |
| NPU 保留 | GP1 控制 0x1F0000000..0x1F000FFFF、NPU→HP0 DDR、完整 NPU 含 resize、PS/网口配置、NPU 中断及复位同步器保留；时钟频率不变 |
| 约束 | 留用引脚保持原位置和电压标准，增加 RS485 引脚；共享 PLL 自动派生，保留 PS IP SDC 与明确复位 / RX 首级例外 |
| 编译、位流与时序 | PLL 派生问题已修复；完整视频对照已生成位流但时序失败，当前精简版结果见 `PL_HEADLESS_20261101.md` |
| 恢复备份 | 既有旧系统备份哈希本轮核对一致；另外通过只读 SSH 流取回当前 `/boot/board` 的 4 个 NPU 启动文件，保存为 `artifacts/pl-takeover-20261101/current-npu-boot-before-pl.tar` 及逐文件 SHA256 清单。部署前须再次检查状态未变 |

脚本对原厂连接逐条检查后替换，任一关键连接不匹配即失败。它保持 placement 的 `opt_timing=high`；这是专用构建配方，不套用只支持默认策略的通用 skill 配方。生成 HDL 上的插入可复现，但在 TD GUI 中重新生成 `design_0` 会覆盖插入；必须重新运行脚本。

## 时钟、复位与地址

本项目全部寄存器、样本 RAM、RTU 状态机与 UART 共用原厂 `pll_75m.clk0_out` 的 **75 MHz**；来自 `design_0_PLL_0/Design.xml` 的 CLK0 实际频率，未照搬独立 UART 示例的 100 MHz。复位来自同域 `rst_n_75m.interconnect_aresetn`，低有效。没有新跨时钟多位数据通路；只有异步 RX 使用两级触发器同步。未来如果拆分时钟域，必须重新设计握手并验证，当前代码不声称支持任意异步 AXI/UART 时钟。

默认 `shared-axi` 方案另接通同一 PLL 已配置的 C1=150 MHz，经新增全局 BUFG 驱动 NPU/GP1/HP0。NPU 和 GP0 使用共享 PLL 锁定条件，各自由原有复位同步器释放复位；失锁会同时复位这两个域。两路相位均为 0°，没有修改 PLL 参数。headless 保留 PS HP1 时钟但将所有事务输入置空闲；视频域只在 `full` 对照配置存在。合同向量为 `fixtures/pl/clock_plan_shared_axi.json` 与 `fixtures/pl/soc_profile_headless.json`。

| 区域 | 已核对物理范围 | Linux / 板端状态 |
|---|---|---|
| 外设整体译码 | 0x80400000..0x8040FFFF | `ps_axi_lite_router` 33 位 BASE/MASK；headless 的其他 GP0 地址返回 DECERR |
| 结果邮箱 | 0x80400000..0x80400FFF | 实际 name=`powerscope_mailbox`，size=0x1000，offset=0 |
| 样本控制与缓存 | 0x80401000..0x80402FFF | 实际 name=`powerscope_waveform`，size=0x2000，offset=0 |
| UIO `/dev/uioN` | 本次结果 `/dev/uio0`、样本 `/dev/uio1` | 每次启动从 sysfs 核对；不假定编号永久不变 |
| IRQ | 不使用 | PS 轮询 ACK；未占用 NPU 中断 |

headless 配套设备树为 `pl/integration/powerscope-headless.dtsi`，包含 UIO 节点并禁用四个已核对路径的视频节点；已在原板 DTB 的副本上编译、回读并验证其他属性不变。`full` 仅用 `powerscope-uio.dtsi`。原厂 DTB 无符号表，补丁使用实际 `/soc` 绝对节点路径。

当前 6.1.111-rt42 内核中的 `uio_pdrv_genirq` 已通过设备专属 `driver_override` 成功绑定两个平台设备。`board/deploy/bind_headless_uio.py` 先校验 live DT 和启动身份，再读取驱动导出的 name/addr/size/offset。板上 platform bus 没有 PCI 风格的 resource 文件，验证依据是 identity ranges 下的 live DT 与实际 UIO map。未安装自动绑定服务；重启须重新执行检查。不得用 4.19.0 的 UIO `.ko` 或 `/dev/mem` 绕过。[Linux 6.1 UIO 驱动源码](https://raw.githubusercontent.com/torvalds/linux/v6.1/drivers/uio/uio_pdrv_genirq.c)

## 结果邮箱与统计口径

相对偏移、字段、flags、类别、误差编号均沿用 ABI 1；完整表仍见 `INTERFACES.md`。

- AW/W 独立缓冲；每个方向允许一笔在途事务，B/R 在背压时保持稳定。pending 支持逐字节 WSTRB；confidence 高 16 位读回 0。非法/未对齐地址和只读寄存器写入返回 SLVERR。
- 同一个 75 MHz 边沿原子锁存全部 pending，并更新 COMMIT/ACK。READY 始终为 1；本实现没有异步提交等待期。ACK 可见时整组数据已生效。
- 新 token 必须为 ACK+1，FFFFFFFF 回绕跳过 0；同 ACK 的重复写是无操作，不刷新超时；其他跳号写返回 SLVERR。复位 ABI/READY/COMMIT/ACK=1/1/0/0。
- 500 ms = 37,500,000 个时钟；有效结果到期后 flags 只保留 SIMULATED，label=FFFF、score/us=0、error=5。新提交恢复。
- RX_VALID_COUNT：本地址、完整 RTU 帧、CRC 正确且可处理的请求，包括非法功能、数量和范围导致的异常响应；03 必须恰好 8 字节。合法范围查询及异常查询均计数。非本地址/广播、短帧、帧间隔/校验/停止位错误、超长 ADU 均不计数。
- RX_CRC_ERROR_COUNT：先过滤本地址，只对 4..256 字节、UART/帧间隔有效而 CRC 错误的帧增加；其他地址坏 CRC 不计数。
- RX_LAST_REQUEST：仅本地址且 CRC 正确的 8 字节 03 请求更新，包含数量/范围异常请求；非法功能不更新。
- 所有计数器 uint32 自然回绕。SNAP_TOKEN 依次经过奇数、三个快照字段更新、偶数；内部事件可以连续到达，不因快照更新丢计数。

## RS485

TX=L17，RX=N16，DE=N15，BANK33 LVCMOS33；与厂商 `05_rs485` 约束核对。J11 的 4/5/6 为 GND/A/B；实物朝向已依据用户提供的 L0/H0/A0/B0 丝印核对，板上 120 Ω 实装情况仍待核实。

接线依据已重新目视核对本机 `MLK_F3P_DR1_250526.pdf` 第 14 页，以及硬件使用手册第 7/21 页：J11 是 12V 电源插座旁的绿色六位 CAN/RS485 共用端子。USB-RS485 的 A→J11-5、B→J11-6、有 GND 时接 J11-4；VCC/5V 不连接，也不连接 FPGA 的 TXD/RXD 逻辑引脚。断电接好后再上电，实物方向见下面的丝印顺序。原理图注明 R120=120 Ω，是否装配仍以实板为准。原始页面与局部图在 `artifacts/pl-headless-20261101/wiring/`。

用户随后报告实物从左往右第 2 位为 L0、第 3 位为 H0，隔一位后为 A0、B0。按这个观察方向，六位依次是 GND / CAN L0 / CAN H0 / GND / RS485 A0 / RS485 B0；接地使用 H0 与 A0 之间的第 4 位，不需要猜测隐藏的 1 脚标记。

固定 115200/8E1、地址 1、功能 03。75 MHz 下每位 651 周期，t1.5=56,250 周期，t3.5=131,250 周期。DE 在首位之前提前一位开启，整个响应期间保持，最后停止位完整结束后再保持一位才关闭。应答开始时锁存整组寄存器，发送中提交新结果不改变当前报文。[Modbus 串行规范](https://www.modbus.org/file/secure/modbusoverserial.pdf)

用户已确认 CH340 COM22 是 USB-RS485 且 A0/B0 已接好。54 项短测请求及响应、CRC/请求计数与 watchdog 过期恢复已通过，诊断 payload 不冒充新 NPU 结果。板载收发器按原理图为 SP3485EN-L/TR，USB 适配器只确认了 CH340 接口芯片；DE 边沿的示波器实测仍未完成。COM10 为 PS 控制台，115200/8N1；COM22 为 RS485，115200/8E1。

## 样本接口 v1

下表偏移以样本 UIO 的 0x80401000 为起点；外设整体 RTL 偏移为表中数值 +0x1000。结果邮箱完全独立。

| 相对偏移 | 方向 | 内容 |
|---|---|---|
| 0x00 | R | 样本 ABI=1 |
| 0x04 | R | 1=可写；2=有未消费返回帧，复位为 1 |
| 0x08 | RW | 输入长度，复位 3032；提交时必须 3032 |
| 0x0C | RW | COMMIT token，ACK+1，回绕跳过 0 |
| 0x10 | R | RETURN/ACK token，完整帧锁定后更新 |
| 0x14 | RW | CONSUME token；只接受当前返回 token，消费后释放缓冲 |
| 0x18 / 0x1C | R | accepted_count / rejected_write_count，uint32 回绕 |
| 0x20 | W | 显式 ABORT=1，只允许清除未提交的部分输入，不允许清除未消费返回帧 |
| 0x24 | R | 顺序写入的 word 数，0..758 |
| 0x1000..0x1BD7 | W/R | 758×32 位同步 RAM：可写态依次完整字写入；返回态只读 |

容量 3032 字节，32 位数据宽。缓存写只支持 WSTRB=F，必须从第 0 字连续写至第 757 字；乱序、重复、部分字、截断提交、占用期间覆写、错误消费 token 都返回 SLVERR 且不改缓存所有权。PS 在数据写完后屏障、提交、等 ACK 和状态 2、屏障、读回、屏障、字节/序号/CRC 验证，最后写 CONSUME。未确认消费前不复用。

PL 不计算 CRC32、不修改字节；CRC32 和 Q15 范围由 PS 验证。仿真中特意注入一帧坏 CRC，RTL 原样返回，PS 拒绝；它不算有效推理输入。复位清所有权和计数，不清 RAM 内容；状态门控保证旧内容不能作为有效返回读出。任何超时或数据不一致后，同一进程保持未解决状态，禁止自动覆盖、重试或 ABORT。

生产服务显式启用 `--recover-interrupted-waveform`，只在新进程启动、取得独占 UIO 锁后处理上次进程退出的遗留输入。稳定、无错误、COMMIT=ACK=CONSUME 且 STATUS=1 的未提交输入可用合同中的 ABORT=1 丢弃；STATUS=2 的完整返回帧必须通过 PSW1/CRC/Q15 校验及稳定 token 检查，才按其 ACK 消费。该帧不进入 NPU，也不发布旧结果。未知所有权、错误计数非零、损坏返回帧仍拒绝启动。恢复前后状态及被丢弃帧身份写入独立启动审计文件和运行清单；生产之外默认不启用此策略。

PS 客户端为 `protocol/waveform_mailbox.py`，UIO 适配器为 `ps/waveform.py`。`ps/app.py --waveform-config` 经 `ps/sample_transport.py` 将返回 Q15 反量化结果交给规则和原生 NPU；字节、CRC、序号或 ACK 错误会终止运行并发布 error=3，不回退到原文件输入。不传此参数仍走原 F32 路径，JSONL/manifest 记录实际 input_transport。5500 窗两路回归及逐项量化差异已记录于部署验收文档。

## 当前实板证据

已完成 headless 启动、实际 UIO 身份与 ABI/ACK、22 类 RS485/异常请求/超时恢复、1100 帧真实样本往返，以及两条输入路径各 5500 窗的原生 NPU 回归。当前长测结果、失败尝试与剩余电气/故障注入边界统一见 [部署验收](VALIDATION.md)，不把历史 preview 或 RTL 记录替换成实板结果。
