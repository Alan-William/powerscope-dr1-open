"""22-class display names, in the frozen model/Modbus class order."""
LABELS = ["正常运行"] + ["{}{} 开关开路".format(phase, i) for phase in ("Ta", "Tb", "Tc") for i in range(1, 5)]
LABELS += ["{} 相传感器{}".format(phase, kind) for kind in ("断线", "卡死", "偏移") for phase in "ABC"]
