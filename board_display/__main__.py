"""Native Qt display. Read-only HTTP; no waveform generator or classifier."""
import argparse
import csv
import json
import math
import sys
import time
from urllib.parse import urlsplit

try:
    from PySide6.QtCore import Qt, QTimer, QRectF, QPointF, QUrl
    from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
    from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QFrame,
        QLabel, QPushButton, QCheckBox, QHBoxLayout, QVBoxLayout, QFileDialog)
    from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply
except ImportError:
    from PyQt5.QtCore import Qt, QTimer, QRectF, QPointF, QUrl
    from PyQt5.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
    from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QFrame,
        QLabel, QPushButton, QCheckBox, QHBoxLayout, QVBoxLayout, QFileDialog)
    from PyQt5.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply

from board_display.state import from_board, from_simulink

COLORS = ("#087f73", "#3264ce", "#bd671c")
STYLE = '''
QWidget { background: #f1f5f9; color: #18334b; font-family: "Microsoft YaHei", "Noto Sans CJK SC", sans-serif; font-size: 14px; }
QLabel { background: transparent; }
QFrame#card { background: white; border: 1px solid #d4dfe8; border-radius: 12px; }
QFrame#card QLabel { border: none; }
QLabel#title { font-size: 26px; font-weight: 700; }
QLabel#hint { color: #5f7385; font-size: 13px; }
QLabel#value { font-size: 25px; font-weight: 700; }
QLabel#result { font-size: 28px; font-weight: 700; }
QLabel#badge { background: #e0f0eb; color: #086a5b; padding: 8px 14px; border-radius: 8px; font-weight: 700; }
QPushButton { min-height: 30px; padding: 6px 18px; background: white; border: 1px solid #b1c4d3; border-radius: 8px; font-weight: 600; }
QPushButton:hover { background: #e4f1ef; border-color: #087f73; }
QPushButton:checked { background: #e0f0eb; color: #086a5b; }
QCheckBox { background: transparent; padding: 6px; spacing: 7px; }
QCheckBox::indicator { width: 20px; height: 20px; }
'''


def label(text, role=None):
    w = QLabel(text)
    w.setTextFormat(Qt.PlainText)
    if role:
        w.setObjectName(role)
    return w


def card():
    w = QFrame()
    w.setObjectName("card")
    layout = QVBoxLayout(w)
    layout.setContentsMargins(18, 14, 18, 14)
    layout.setSpacing(8)
    return w, layout


class Waveform(QWidget):
    """Adapted from the existing qt_demo QPainter display, with 25 ms input windows."""
    def __init__(self):
        super().__init__()
        self.setMinimumHeight(210)
        self.frame = None
        self.visible_channels = [True] * 3

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor("white"))
        plot = QRectF(62, 24, max(100, self.width()-80), max(100, self.height()-66))
        values = self.frame["currents"] if self.frame else []
        peak = max((abs(v) for row in values for v in row), default=1)
        limit = max(.1, math.ceil(peak * 1.12 * 10) / 10)
        p.setFont(QFont("Segoe UI", 10))
        for i in range(5):
            y = plot.top() + plot.height() * i / 4
            p.setPen(QPen(QColor("#e1e8ee"), 1))
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            p.setPen(QColor("#657a8c"))
            p.drawText(QRectF(0, y-10, 51, 20), Qt.AlignRight | Qt.AlignVCenter, "{:g}".format(limit*(1-i/2)))
        for i in range(6):
            x = plot.left() + plot.width()*i/5
            p.setPen(QPen(QColor("#e1e8ee"), 1))
            p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            p.setPen(QColor("#657a8c"))
            p.drawText(QRectF(x-20, plot.bottom()+8, 40, 20), Qt.AlignCenter, str(i*5))
        p.drawText(5, 15, self.frame["unit"] if self.frame else "I")
        p.drawText(self.width()-24, self.height()-3, "ms")
        if not values:
            p.drawText(plot, Qt.AlignCenter, "等待电流窗口")
        p.save()
        p.setClipRect(plot.adjusted(-1, -1, 1, 1))
        for channel, row in enumerate(values):
            if not self.visible_channels[channel]:
                continue
            # Each window has samples [0, 25 ms); the final sample is not at 25 ms.
            points = QPolygonF([QPointF(plot.left()+i*plot.width()/len(row),
                plot.center().y()-v*plot.height()/(2*limit)) for i, v in enumerate(row)])
            p.setPen(QPen(QColor(COLORS[channel]), 2.1, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPolyline(points)
        p.restore()
        p.end()


class Dashboard(QMainWindow):
    def __init__(self, api, preview=False):
        super().__init__()
        self.preview = preview
        self.api = api
        self.frame = None
        self.frozen = False
        self.events = []
        self.last_label = None
        self.last_key = None
        self.changed_at = time.monotonic()
        self.state = {}
        self.setWindowTitle("PowerScope | DR1 波形与故障诊断" + (" · 本机预览" if preview else ""))
        self.resize(1280, 720)
        self.setMinimumSize(960, 570)
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(22, 16, 22, 14)
        root.setSpacing(12)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.addWidget(label("功率变换器智能故障诊断系统", "title"))
        titles.addWidget(label("PowerScope DR1  /  三相电流 · 在线诊断", "hint"))
        header.addLayout(titles)
        header.addStretch()
        self.mode = label("本机 Simulink 波形预览" if preview else "sfaug-r1 · FPGA NPU", "badge")
        header.addWidget(self.mode)
        root.addLayout(header)
        self.status = label("连接数据服务…", "hint")
        root.addWidget(self.status)
        metrics = QHBoxLayout()
        self.rms_labels = []
        for i, name in enumerate(("A", "B", "C")):
            panel, layout = card()
            title = label(name + " 相电流 RMS · 20 ms")
            title.setStyleSheet("color: " + COLORS[i])
            value = label("—", "value")
            layout.addWidget(title)
            layout.addWidget(value)
            self.rms_labels.append(value)
            metrics.addWidget(panel)
        root.addLayout(metrics)
        body = QHBoxLayout()
        plot_panel, plot_layout = card()
        plot_header = QHBoxLayout()
        plot_header.addWidget(label("三相电流波形"))
        plot_header.addStretch()
        self.wave = Waveform()
        for i, name in enumerate(("Ia", "Ib", "Ic")):
            toggle = QCheckBox(name)
            toggle.setChecked(True)
            toggle.setStyleSheet("color: " + COLORS[i])
            toggle.toggled.connect(lambda checked, channel=i: self.channel(channel, checked))
            plot_header.addWidget(toggle)
        plot_layout.addLayout(plot_header)
        plot_layout.addWidget(self.wave, 1)
        self.caption = label("20 kHz  /  500 点每相  /  25 ms 窗口", "hint")
        plot_layout.addWidget(self.caption)
        body.addWidget(plot_panel, 7)
        result_panel, result_layout = card()
        result_panel.setMinimumWidth(280)
        self.result_title = label("仿真波形预览" if preview else "当前诊断 · 纯 NPU", "hint")
        result_layout.addWidget(self.result_title)
        self.result = label("接收电流窗口" if preview else "等待输入", "result")
        self.result.setWordWrap(True)
        result_layout.addWidget(self.result)
        self.detail = label("电脑控制故障注入" if preview else "模型  sfaug-r1", "hint")
        self.detail.setWordWrap(True)
        result_layout.addWidget(self.detail)
        self.score = label("输入采样  20 kHz" if preview else "模型得分  —")
        self.latency = label("窗口长度  25 ms" if preview else "NPU 推理  —")
        self.sequence = label("帧序号  —")
        for w in (self.score, self.latency, self.sequence):
            result_layout.addWidget(w)
        result_layout.addStretch()
        result_layout.addWidget(label("运行记录", "hint"))
        self.event_label = label("等候数据流", "hint")
        self.event_label.setWordWrap(True)
        self.event_label.setMinimumHeight(74)
        result_layout.addWidget(self.event_label)
        body.addWidget(result_panel, 3)
        root.addLayout(body, 1)
        footer = QHBoxLayout()
        self.freeze = QPushButton("冻结画面")
        self.freeze.setCheckable(True)
        self.freeze.toggled.connect(self.toggle_freeze)
        footer.addWidget(self.freeze)
        export = QPushButton("保存当前窗口")
        export.clicked.connect(self.export)
        footer.addWidget(export)
        footer.addStretch()
        self.timestamp = label("Simulink → 网口 → DR1 → HDMI", "hint")
        footer.addWidget(self.timestamp)
        fullscreen = QPushButton("全屏")
        fullscreen.clicked.connect(self.toggle_fullscreen)
        footer.addWidget(fullscreen)
        root.addLayout(footer)
        self.manager = QNetworkAccessManager(self)
        self.reply = None
        self.timeout = QTimer(self)
        self.timeout.setSingleShot(True)
        self.timeout.timeout.connect(self.abort_request)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(250)
        QTimer.singleShot(0, self.poll)

    def channel(self, index, checked):
        self.wave.visible_channels[index] = checked
        self.wave.update()

    def toggle_fullscreen(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_F11:
            self.toggle_fullscreen()
        elif event.key() == Qt.Key_Escape and self.isFullScreen():
            self.showNormal()
        else:
            super().keyPressEvent(event)

    def toggle_freeze(self, checked):
        self.frozen = checked
        self.freeze.setText("继续刷新" if checked else "冻结画面")
        if checked:
            self.status.setText("画面已冻结 · 点击“继续刷新”查看最新数据")
        else:
            self.last_key = None

    def poll(self):
        if self.reply is not None:
            return
        path = "/api/simulation/state?display=matlab" if self.preview else "/api/state"
        self.reply = self.manager.get(QNetworkRequest(QUrl(self.api + path)))
        self.reply.finished.connect(self.received)
        self.reply.downloadProgress.connect(self.progress)
        self.timeout.start(1500)

    def progress(self, received, total):
        if received > 2 * 1024 * 1024 or total > 2 * 1024 * 1024:
            self.abort_request()

    def abort_request(self):
        if self.reply is not None:
            self.reply.abort()

    def received(self):
        reply, self.reply = self.reply, None
        self.timeout.stop()
        try:
            if reply.error() != QNetworkReply.NoError:
                raise ValueError("数据服务连接中断")
            raw = bytes(reply.readAll())
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("状态报文长度异常")
            self.state = json.loads(raw.decode("utf-8"))
            frame = from_simulink(self.state) if self.preview else from_board(self.state)
            if self.frozen:
                return
            self.present(frame)
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as error:
            self.status.setText(str(error) + (" · 保留上一帧" if self.frame else " · 等候连接"))
            self.result_title.setText("波形预览" if self.preview else "上一帧诊断" if self.frame else "当前诊断")
            self.status.setStyleSheet("color: #ac4b29")
        finally:
            reply.deleteLater()

    def present(self, frame):
        running = self.state.get("status") in ("starting", "running", "finalizing", "stopping")
        if frame is None:
            self.frame = self.wave.frame = None
            self.last_key = self.last_label = None
            self.events = []
            self.wave.update()
            self.result.setText("接收电流窗口" if self.preview else "等待输入")
            self.score.setText("输入采样  20 kHz" if self.preview else "模型得分  —")
            self.latency.setText("窗口长度  25 ms" if self.preview else "NPU 推理  —")
            self.sequence.setText("帧序号  —")
            for value in self.rms_labels:
                value.setText("—")
            self.event_label.setText("等候数据流")
            self.result_title.setText("仿真波形预览" if self.preview else "当前诊断 · 纯 NPU")
            self.result.setStyleSheet("color: #18334b")
            self.detail.setText("电脑控制故障注入" if self.preview else "模型  sfaug-r1")
            self.timestamp.setText("Simulink → 网口 → DR1 → HDMI")
            self.status.setStyleSheet("color: #5f7385")
            self.status.setText("等候电脑电流输入" if self.state.get("status") != "error" else "输入已停止 · 等候重新启动")
            return
        if frame["key"] != self.last_key:
            if self.last_key and frame["key"][:-1] != self.last_key[:-1]:
                self.events = []
                self.last_label = None
            self.changed_at = time.monotonic()
            self.last_key = frame["key"]
            if frame["label"] is not None and frame["label"] != self.last_label:
                stamp = "{:.3f}s".format(frame["sim_time_s"]) if frame["sim_time_s"] is not None else "#{}".format(frame["sequence"])
                self.events.append(stamp + "  " + frame["label_name"])
                self.events = self.events[-3:]
                self.last_label = frame["label"]
            self.frame = self.wave.frame = frame
            self.wave.update()
            for target, rms in zip(self.rms_labels, frame["rms"]):
                target.setText("{:.2f} {}".format(rms, frame["unit"]))
            self.sequence.setText("帧序号  #{}".format(frame["sequence"]))
            if self.preview:
                self.result.setText("Simulink 电流")
                self.detail.setText("本机显示预览\n当前画面来自电路仿真输出")
                self.event_label.setText("25 ms 电流窗口\nA / B / C 三相同步显示")
            else:
                self.result.setText(frame["label_name"])
                self.result.setStyleSheet("color: " + ("#087f73" if frame["label"] == 0 else "#b35421"))
                self.detail.setText("PL 输入 → NPU 推理 → AXI 发布")
                self.score.setText("模型得分  {:.2%}".format(frame["score"]))
                self.latency.setText("NPU 推理  {:.3f} ms".format(frame["inference_us"]/1000))
                self.event_label.setText("\n".join(reversed(self.events)))
            point_text = "20 kHz  /  500 点每相  /  25 ms 窗口"
            self.caption.setText(point_text + ("  /  归一化电流" if frame["unit"] == "p.u." else ""))
            self.timestamp.setText("仿真时刻  {:.3f} s".format(frame["sim_time_s"]) if frame["sim_time_s"] is not None else "同帧波形与诊断结果")
        age = time.monotonic() - self.changed_at
        self.status.setStyleSheet("color: #ac4b29" if running and age > 2 else "color: #5f7385")
        self.result_title.setText("仿真波形预览" if self.preview else "当前诊断 · 纯 NPU" if running and age <= 2 else "上一帧诊断 · 纯 NPU")
        if self.state.get("status") == "error":
            self.status.setText("数据流已停止 · 保留最后一帧")
        elif not running:
            self.status.setText("仿真已停止 · 保留最后一帧" if self.preview else "输入已停止 · 保留最后一帧")
        elif age > 2:
            self.status.setText("等候新电流窗口 · 上次更新 {:.1f} s 前".format(age))
        else:
            self.status.setText("本机 Simulink 波形预览" if self.preview else "接收电脑仿真输入 · 显示板端同帧诊断")

    def export(self):
        frame = self.frame
        if not frame:
            return
        name, _ = QFileDialog.getSaveFileName(self, "保存当前显示窗口", "powerscope-frame-{}.csv".format(frame["sequence"]), "CSV (*.csv)")
        if not name:
            return
        try:
            with open(name, "w", newline="", encoding="utf-8-sig") as stream:
                writer = csv.writer(stream)
                writer.writerow(["time_ms", "Ia_"+frame["unit"], "Ib_"+frame["unit"], "Ic_"+frame["unit"], "sequence", "npu_label"])
                for i, values in enumerate(zip(*frame["currents"])):
                    writer.writerow([i*25/len(frame["currents"][0]), *values, frame["sequence"], frame["label"]])
            self.status.setText("当前窗口已保存")
        except OSError as error:
            self.status.setText("保存失败：" + str(error))

    def closeEvent(self, event):
        self.timer.stop()
        self.abort_request()
        event.accept()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8765")
    parser.add_argument("--preview-simulink", action="store_true", help="Laptop-only actual Simulink waveforms, no diagnosis")
    parser.add_argument("--fullscreen", action="store_true")
    args = parser.parse_args()
    api = "http://127.0.0.1:18768" if args.preview_simulink else args.api.rstrip("/")
    parsed = urlsplit(api)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost") or parsed.path or parsed.query or parsed.fragment or parsed.username:
        parser.error("Use the local board service or an existing localhost SSH tunnel")
    app = QApplication([sys.argv[0]])
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    window = Dashboard(api, args.preview_simulink)
    window.showFullScreen() if args.fullscreen else window.show()
    return app.exec() if hasattr(app, "exec") else app.exec_()


if __name__ == "__main__":
    sys.exit(main())
