# ABI v1（PS 已接入，PL 与实物链路待联调）

2026-11-01 PL 接手更新：结果 ABI 偏移和黄金向量保持不变；新增 RTL、计数口径、独立样本缓冲及候选共存地址见 [硬件合同](hardware-contract.md)。RTL 仿真已覆盖，物理 UIO/RS485 和共存 NPU 尚待验收；以下历史“PL 待实现”描述现应区分为代码已有、实物未验收。

## 数据与模型

当前选择 `kd-s1` 蒸馏学生。PS 回放文件每帧 6000 字节：little-endian float32，A/B/C 各 500 点，逻辑 shape `[1,3,500]`。输入已经经过仿真数据管线的离线低通、加噪和每窗三相共同最大绝对值归一化；不得再单相归一化，也不得套用图像 RGB 均值/比例。模型/数据哈希见资产 manifest，打包入口为 `tools/package_ps_assets.py`。

PS worker 将各相 500 点写到对应 32×32 平面的前 500 个位置，其余补零；输入 `[1,9,32,32]` 的后六个平面全零，**不计算统计特征**。Alnpu 卷积主干输出 `[1,32,4,4]`，共享 CMA 缓冲区以 `[1,512]` 视图输入 Alnpu 分类头；没有 CPU 神经算子或回退。输出 `[1,22]` logits，PS argmax/softmax 得类别和未校准分数。

PS→worker 的进程内协议为 ASCII `PSF1`、uint32 LE sequence、6000 字节原始 F32，合计 6008 字节。worker stdout 每帧一行 JSON。这个协议不是 AXI 或 RS485 线协议。

历史 `fixtures/golden_q15.bin`：22 帧，每帧 3000 字节，little-endian int16，A 的 500 点后接 B、C。量化 `round(x*32767)`，范围 -32767..32767，反量化除以 32767。Q15 仅为传输格式，与 NPU INT8/UINT8 量化不同。`golden_f32.bin` 是相同帧顺序的归一化 float32。历史参考 logits 属于 ECA 基线，**不能拿来判定 kd-s1 正误**。当前学生报文样例为 `fixtures/student/wire_vectors.json`，包含真实 NPU 结果与预期线协议字节，但不构成实际传输证据。

类别沿用上游编号：0 Normal；1–12 依次 Ta1..Ta4、Tb1..Tb4、Tc1..Tc4 OS；13–15 SensorA/B/C DF；16–18 SF；19–21 OF。缩写含义以原仿真故障定义为准，暂不在 UI 中擅自改标签。

PS可显式启用 `sensor-flat-v1` 规则辅助，规则只读输入波形，每帧仍运行Alnpu。最终类别可能由规则修正；原始NPU类别/logits和规则来源另行保留。NPU统计输入和PL统计模块不因此启用，详见 [规则说明](../experiments/student/README.md)。

## Modbus RTU

115200，正式RTU采用8E1（可选无校验8N2），地址1，功能03。厂商8N1回显仅供电气调试，两端切换RTU时同步更改格式。固定高波特率 t1.5=750 µs、t3.5=1.75 ms；以实际板上时钟计算定时器。依据：[Modbus Serial Line Guide V1.02](https://www.modbus.org/file/secure/modbusoverserial.pdf)。CRC16 初值 FFFF、多项式 A001，CRC 低字节先发；寄存器字节序大端，32 位值高16位寄存器在前。

| 保持寄存器（零基） | 内容 |
|---|---|
| 0 | ABI_VERSION=1 |
| 1 | flags：bit0 VALID，bit1 NPU_EXECUTED，bit2 SIMULATED，其余0 |
| 2 | label 0..21；无有效结果为 FFFF |
| 3 | confidence，PS round(NPU对最终发布类别的softmax分数*10000)，0..10000，未经校准；规则修正后可能很低或为0，不代表规则概率 |
| 4,5 | frame_seq，高字/低字 |
| 6,7 | inference_us，高字/低字，两个 Enqueue 及中间 CMA 同步，不含输入准备、HTTP、落盘或 RTU |
| 8,9 | application_error，高字/低字，0为无错误 |

请求0..9示例：`01 03 00 00 00 0A C5 CD`。开始应答时锁存整组结果；不得出现新旧帧字段混合。初始 flags=0，label=FFFF。NPU_EXECUTED 只能在有实际后端证据的路径设置，CPU离线回放固定清零。

NPU_EXECUTED表示实际执行，规则修正后仍可置位；不得将其解释为“类别必须是NPU argmax”。ABI 1没有规则来源位，仍保持其余flags位为0。PS日志/网页记录来源，队友原子传递最终类别与对应分数即可；新版对照向量在 `fixtures/student/rule_wire_vectors.json`。

PS 本轮真实 NPU 结果 flags=7；开始、正常结束或停止时提交 flags=4、label=FFFF、score=0、inference_us=0、error=0。异常也清除 VALID/NPU_EXECUTED，error：1=NPU，2=存储，3=AXI，4=输入。frame_seq 在一个服务进程内递增并按 uint32 回绕，重启可从 0 开始；跨进程追溯用存储记录中的 session/run_id，不能只用 sequence。

PL 待实现的掉线保护：默认 500 ms 无新提交且当前 VALID=1，清除 VALID/NPU_EXECUTED、label=FFFF、score/inference_us=0，保留 SIMULATED，error=5（PS 发布超时）。新提交恢复正常。500 ms 做成 RTL 参数；25/250 ms 回放适用，超过此间隔的慢速回放会在帧间进入无效状态。该保护尚未上板验证，不改变当前相对寄存器偏移。PS 无法 ACK 时不会冒险覆写未确认的 pending，不能依赖软件一定能清除旧结果。

错误地址/CRC、短帧、广播读丢弃。非法功能返回异常01，越界寄存器返回02，数量0或>125返回03。计数饱和或32位回绕策略需要PL明确（建议无符号回绕，PS按差值处理）。

## AXI-Lite mailbox

32位，little-endian，byte offsets，支持标准 AW/W 独立握手和 WSTRB。物理基址、IRQ、UIO/驱动节点待队友分配。当前不提供不安全的猜测地址访问脚本。

| 偏移 | 方向 | 内容 |
|---|---|---|
| 0x00 | R | ABI=1 |
| 0x04 | R | bit0 READY（提交ACK与PS一致后为1），其余待定为0 |
| 0x08 | RW | PS 写 pending sequence |
| 0x0C | RW | pending flags[15:0]，label[31:16] |
| 0x10 | RW | pending confidence[15:0]，高16位必须0 |
| 0x14 | RW | pending inference_us |
| 0x18 | RW | pending application_error |
| 0x1C | RW | COMMIT_TOKEN，PS从当前ACK+1取值；uint32回绕时跳过0 |
| 0x20 | R | ACK_TOKEN，PL原子复制pending后更新；PS等待ACK才覆写pending |
| 0x24 | R | RX_VALID_COUNT（有效本从站请求数量） |
| 0x28 | R | RX_CRC_ERROR_COUNT |
| 0x2C | R | RX_LAST_REQUEST（高16=start、低16=count） |
| 0x30 | R | SNAP_TOKEN（PL更新最近请求信息前变奇数，完成后变偶数） |

复位 token=0、ACK=0、READY=1，诊断结果无效；PS写完所有pending字段后做设备内存写屏障，最后写COMMIT_TOKEN；PL锁存结果并ACK。若跨时钟域，使用握手和稳定数据窗口，不能只同步单个多位总线。PL更新请求字段前先使SNAP_TOKEN为奇数，写完所有字段后再变偶数；PS在读屏障前后读取SNAP_TOKEN，只有两次相同且为偶数才接受，否则重读。PL必须保证token变化对AXI读可见的次序，或改用读锁存命令并更新ABI。读超时/提交超时必须报错，不能无限阻塞。

不通过 AXI 或 RS485 传输整窗波形：波形当前留在 PS DDR/文件，AXI交换控制与诊断结果。这条路径仍须实测 PS→PL 与 PL→PS 双向数据变化。

以上只描述**当前结果邮箱 ABI 1**，不代表已经实现题目所需的完整波形数据通路。下一阶段样本通道的独立帧格式、交付和验收见 [AXI 波形通道提案](hardware-contract.md)，不得混用结果邮箱地址或宣称已完成。

`ps/axi.py` 默认 preview，完全不映射硬件。启用 UIO 必须填写 `configs/axi-uio.template.json`，同时提供真实设计来源、UIO 名称、map0 地址/长度；程序逐项核对 sysfs，拒绝非零 map offset、错误 ABI 和未 ACK 的提交。ARM64 `libpowerscope_mmio.so` 负责对齐 volatile 32 位访问和 `dmb sy` 屏障。每次 ACK/稳定快照等待上限 50 ms。测试假寄存器仅证明软件逻辑，实际时钟域、AW/W、WSTRB、内存属性和 ACK 可见性由联合测试验收。
