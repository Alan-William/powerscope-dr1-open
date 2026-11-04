# Simulink 输入与 DR1 HDMI 触摸屏演示

电脑运行原 NPC Simulink 电路，控制工况、故障注入与恢复。DR1 接收三相电流窗口，完成 PL 输入交接、sfaug-r1 NPU 推理与 AXI 发布，再在自己的 HDMI 屏幕显示这一帧的电流和诊断结果。USB 触摸用于板端界面操作；RS485 回传由电脑保存并核对。

## 沿用的显示系统

此前板端 Qt 界面和实机记录已定位：

- 原 Qt 工程：`E:/00比赛/嵌赛FPGA/qt_demo`，历史板端目录 `/home/uisrc/powerscope`。
- 已用环境：Ubuntu 20.04 ARM64、PyQt5 5.14.1 / Qt 5.12.8、X11 fbdev。
- 2026-10-22 的 `Xorg.0.log` 记录 `/dev/fb0`、1920×1080 输出、148.5 MHz 像素时钟。屏幕配套资料注明面板分辨率 1024×600；界面适配以实际 framebuffer 模式为准。
- USB 触摸设备是 `wch.cn USB2IIC_CTP_CONTROL`（VID:PID 1A86:E5E3），当次由 libinput 识别为 touchscreen。设备节点当时为 `/dev/input/event0`，当前 Qt 显示服务也通过输入系统发现触摸设备，不固定绑定 event 序号。
- 当时 Qt 全屏设置为 `QT_SCALE_FACTOR=1.5`。旧界面和配置保留在原目录；新版本源代码独立置于本仓库 `board_display/`。

## 接线与分工

| 连接 | 用途 |
|---|---|
| 电脑 ↔ DR1 网口 | 传输电流窗口与控制会话 |
| DR1 HDMI 输出 → 屏幕 HDMI 输入 | 板端界面与波形 |
| 屏幕触摸 USB → DR1 USB 主机口 | 触摸操作，沿用原有接法 |
| USB-RS485 → 电脑；A/B → DR1 A0/B0 | 接收板端诊断报文 |
| DR1、屏幕电源 | 按设备原有供电方式连接 |

在电脑选择故障后，改变的是 Simulink 门极或测量通路。系统继续求解当前电路状态，并按处理速度选取最新完整窗口送入 DR1。屏幕展示 DR1 实际处理的窗口，而电脑 Scope 可以显示更密集的仿真波形。每个窗口仍是 25 ms 仿真时间；屏幕刷新率、诊断吞吐率与 20 kHz 输入采样率分别表示不同环节。

## 画面与操作

新 Qt 界面沿用原生 Widgets / QPainter，采用浅色底、白色面板及青绿、蓝、暖橙三相颜色。主画面包括三相电流、最近 20 ms 电流 RMS、故障类别、NPU 推理时间、模型得分、帧序号和最近类别变化。RMS 时间范围与 MATLAB 控制台一致，电流图和分类窗口仍为 25 ms。模型得分来自 NPU softmax，不作为准确率或校准概率。

“冻结画面”保持当前显示窗口；“继续刷新”回到最新帧；Ia / Ib / Ic 开关选择显示通道；“保存当前窗口”导出当前画面的数据；“全屏”适配触摸屏。数据服务断开或输入停止后，保留的画面明确标为上一帧。

### 本机预览

1. 启动桌面 `PowerScope Simulink`，或 `tools/start_simulink_demo.cmd`。
2. 双击 `tools/start_board_display_preview.cmd`。这会打开板端布局的原生 Qt 预览，读取本机 Simulink 接收服务。
3. 在 MATLAB 中启动仿真、注入开路或传感器故障；预览窗口显示相同数据流的最新电流窗口。停止仿真后保留最后一帧。

本机预览显式标识数据来源，显示真实 Simulink 输出，不产生诊断类别。生产模式则只读取板上 `http://127.0.0.1:8765/api/state` 的同帧结果，且要求 sfaug-r1、规则关闭、NPU 执行标记和物理 PL / AXI 回执一致。

### 板端启动

HDMI 能力镜像和生产服务核验后，由 `powerscope-display.service` 启动新包的 `board/deploy/run_display.sh`。服务在 tty7 使用 Qt LinuxFB，将画面直接绘制到 `/dev/fb0`，以 `uisrc` 用户运行，并通过 `video`、`input` 组读取屏幕和触摸设备。systemd 分配控制终端后，Qt 通过 `/dev/tty` 打开它；启动前切换到 tty7。程序从已有 PS 服务读取状态，不直接操作 MMIO 或采集设备。其 Python 代码兼容板上 3.8 语法，继续使用既有 PyQt5。

新显示程序位于 `/opt/powerscope-display/releases/`，`current` 指向本轮以清单哈希命名的发布目录。PS 波形元数据更新采用独立运行目录和 systemd 覆盖配置；sfaug-r1 资产、NPU worker 及原发布目录保留原字节内容。启动文件、UIO 绑定器与服务配置的恢复副本位于 `/var/backups/powerscope-hdmi-20261104/`。原桌面配置和历史 Qt 工程保持可用作参考。

`tools/package_board_display.py` 生成独立的显示程序包和 SHA256 清单，解包使用新目录，例如 `/home/uisrc/powerscope-display-20261104`。启动器从自身位置定位包；历史 `/home/uisrc/powerscope` 保留。该包只包含显示及其解析依赖，PS 服务升级使用本仓库生产发布流程，模型资产继续使用 sfaug-r1。

## 数据接口

板端 `/api/state.latest` 在同一次加锁更新中发布波形、帧序号、NPU 结果和物理链路回执。新版本提供完整三相各 500 点的 PL 返回电流，与实际送入 NPU 的归一化样本一致。界面不会将独立轮询得到的某帧波形与另一帧结果拼接。

发送器在服务声明 `external_input.display_metadata_version=1` 后，才添加可选 `display` 字段：

```json
{"scale_a": 4.32, "sample_rate_hz": 20000, "sim_time_s": 3.025}
```

`scale_a` 是这一个窗口三相共同归一化所用的幅值，屏幕将 PL 返回样本乘回该幅值得到安培值，因此包含 Q15 传输量化影响。`sim_time_s` 表示窗口结束的仿真时刻。三个字段仅用于显示，不进入 NPU 输入或分类逻辑。请求仍不携带故障类别、参考答案或预设诊断。旧服务保持原四字段协议，显示单位使用 p.u.。

## HDMI 工程衔接

2026-11-01 部署的 `236df82` 精简工程保留 NPU、网口、AXI 和 RS485；按当时范围去除了 HDMI。恢复板端显示时，需要带 HDMI 的配套 PL 和设备树，不能仅重启 Qt 或单独启用设备树节点。

恢复范围为原 framebuffer → VDMA MM2S → 视频时序 → HDMI 输出，并保留当前 PowerScope 外设与 NPU 通路。原设备树中的显示节点为 `/soc/vdma@80200000`、`/soc/vdmafb@80100000`、`/soc/axi_dynclk@80101000`。地址来自归档和已有设备树，不用于猜测寄存器写入。摄像头保持停用。

接板后的联调顺序：核对当前启动文件及恢复入口 → 对 HDMI 候选执行时钟、跨域复位和布局布线时序检查 → 部署配套镜像与设备树 → 检查 framebuffer 和触摸输入 → 启动纯 NPU 服务及 Qt → 短流程验证正常、故障、恢复。当前 NPU 150 MHz 和外设 75 MHz 的已验收版本保持可回退。

旧显示证据位于 `E:/00比赛/嵌赛FPGA/触摸屏资料/板端测试_2026-10-22`，当前精简工程证据见 [PL_HEADLESS_20261101.md](BUILD.md)。本轮的本机预览和接口检查记录独立保存，板端屏幕运行以接板后的实测记录为准。

Qt 实现采用官方 [QNetworkAccessManager](https://doc.qt.io/qt-6/qnetworkaccessmanager.html) 异步读取和 [QPainter](https://doc.qt.io/qtforpython-6.11/PySide6/QtGui/QPainter.html) 绘图；每次仅一个在途读取，设超时并限制响应长度。
