# sfaug-r1 轻量故障诊断模型

本目录提供轻量网络、量化感知训练、传感器卡死增强及评估代码。正式选择模型为 `aug_ce-s0`（对外标识 `sfaug-r1`），参数、量化信息与训练记录见 `models/sfaug-r1/`，板端直接使用 `assets/trunk3.onnx` 和 `assets/head.onnx`。

`train_sensor_augmentation.py` 比较 CE、增强 CE 与增强 KD，采用预先冻结的验证协议选择模型；选择记录 `selection_uses_test=false`。核心代码还包括 `network.py`、`export_qdq.py`、`augment_sensor.py` 及 `evaluate_sensor_augmentation.py`。

网络将三相输入分别映射到 32×32 布局，三个卷积块提取特征，再通过 512→22 分类头输出类别。部署参数量为 26,486，trunk/head 在板端同为 Alnpu 后端。

训练需要 Python、NumPy、PyTorch、ONNX 与 ONNX Runtime；依赖版本应与选定训练环境匹配。通过各脚本的 `--help` 指定数据、检查点及输出目录。完整实验输入单独管理，公开仓库提供复现实验逻辑、训练协议、所选权重和演示小样本。

历史 PS 规则相关脚本保留为研究对照。正式模型及本仓库 README 所述准确率采用纯 NPU 输出，不启用规则覆盖。
