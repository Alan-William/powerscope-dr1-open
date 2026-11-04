"""PC waveform sender. Predictions displayed here come from physical RS485 bytes."""
import base64
from collections import deque
import hashlib
import json
import math
from pathlib import Path
import secrets
import struct
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pc_monitor.monitor import LABELS, utc_now

STEPS = [(0, 0), (3, 2), (6, 0), (9, 16), (12, 0), (15, 21), (18, 0)]
ACTIVE = {"starting", "running", "stopping"}


class WindowLibrary:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.manifest = json.loads(self.path.read_text(encoding="utf-8"))
        m = self.manifest
        raw_path = (self.path.parent / m["path"]).resolve()
        if self.path.parent not in raw_path.parents:
            raise ValueError("Signal file must be inside its asset directory")
        self.raw = raw_path.read_bytes()
        if (m.get("schema") != 1 or m.get("format") != "float32-le-phase-major-normalized"
                or m.get("channels") != 3 or m.get("samples_per_channel") != 500
                or m.get("sample_rate_hz") != 20000 or m.get("simulated") is not True
                or hashlib.sha256(self.raw).hexdigest() != m["sha256"]
                or len(self.raw) != m["count"] * 6000
                or len(m["labels"]) != m["count"] or len(m["dataset_indices"]) != m["count"]
                or any(type(x) is not int or not 0 <= x < 22 for x in m["labels"])):
            raise ValueError("Signal library format, length or hash mismatch")
        self.pools = {label: [i for i, x in enumerate(m["labels"]) if x == label] for label in range(22)}
        if any(not indices for indices in self.pools.values()):
            raise ValueError("Signal library must include all 22 classes")
        self.positions = {label: 0 for label in self.pools}

    def next(self, label):
        pool = self.pools[label]
        position = self.positions[label]
        index = pool[position % len(pool)]
        raw = self.raw[index * 6000:(index + 1) * 6000]
        values = struct.unpack("<1500f", raw)
        if not all(math.isfinite(v) and -1 <= v <= 1 for v in values):
            raise ValueError("Source window contains non-normalized or nonfinite samples")
        self.positions[label] += 1
        return raw, dict(source_index=index, dataset_index=self.manifest["dataset_indices"][index],
                         source_cycle=position // len(pool), source_label=label,
                         source_name=LABELS[label], sha256=hashlib.sha256(raw).hexdigest(),
                         waveform=[list(values[c * 500:(c + 1) * 500:5]) for c in range(3)])


class BoardClient:
    def __init__(self, base="http://127.0.0.1:18765"):
        self.base = base
        self.token = None

    def request(self, path, body=None, timeout=5):
        headers = {"Content-Type": "application/json"}
        if body is not None:
            headers["X-PowerScope-Token"] = self.token or ""
        request = Request(self.base + path, data=None if body is None else json.dumps(body).encode(), headers=headers)
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except HTTPError as error:
            try:
                message = json.load(error).get("error", str(error))
            except ValueError:
                message = str(error)
            raise OSError("板端拒绝操作：" + message) from error
        except (URLError, TimeoutError) as error:
            raise OSError("网口未连通：检查 DR1 电源、网线和本机 SSH 隧道（18765）") from error

    def state(self):
        state = self.request("/api/state")
        self.token = state["csrf"]
        return state


class Demo:
    def __init__(self, monitor, assets, output, board=None, simulator=None):
        self.monitor = monitor
        self.assets = Path(assets)
        self.output = Path(output)
        self.board = board or BoardClient()
        self.simulator = simulator
        self.source_kind = "library"
        self.lock = threading.RLock()
        self.thread = None
        self.stop_event = threading.Event()
        self.status = "idle"
        self.error = ""
        self.library_error = ""
        self.library = None
        try:
            self.library = WindowLibrary(self.assets)
        except (OSError, ValueError, KeyError) as error:
            self.library_error = "演示波形库未准备好：" + str(error)
        self.session = None
        self.path = None
        self.log_size = 0
        self.run_id = None
        self.auto = False
        self.requested_label = 0
        self.sent = self.matched = 0
        self.latest_input = self.latest_match = None
        self.events = deque(maxlen=40)
        self.start_mono = None
        self.elapsed = 0
        self.board_info = None

    def snapshot(self):
        with self.lock:
            return dict(status=self.status, error=self.error,
                        available=bool(self.simulator) if self.source_kind == "live" else bool(self.library),
                        library_error="" if self.source_kind == "live" else self.library_error, labels=LABELS, session=self.session,
                        source="NPC 在线电路仿真" if self.source_kind == "live" else self.library.manifest["title"] if self.library else None,
                        source_kind=self.source_kind,
                        run_id=self.run_id, automatic=self.auto, requested_label=self.requested_label,
                        sent=self.sent, matched=self.matched, latest_input=self.latest_input,
                        latest_match=self.latest_match, events=list(self.events),
                        elapsed_s=round(time.monotonic() - self.start_mono, 1) if self.status == "running" and self.start_mono else self.elapsed,
                        board=self.board_info, duration_s=21,
                        steps=[dict(at_s=t, label=y, name=LABELS[y]) for t, y in STEPS],
                        export=f"/api/demo/export?session={self.session}" if self.path else None)

    def start(self, port, automatic=False, source="library"):
        if type(automatic) is not bool:
            raise ValueError("automatic must be boolean")
        if type(source) is not str or source not in {"library", "live"}:
            raise ValueError("请选择有效信号源")
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError("演示正在运行")
            if source == "library" and not self.library:
                raise ValueError(self.library_error)
            if source == "live" and (not self.simulator or self.simulator.snapshot()["status"] != "running"):
                raise ValueError("请先启动在线仿真")
            self.source_kind = source
            self.status, self.error = "starting", ""
            self.auto = automatic if source == "library" else False
            self.requested_label = 0
            self.sent = self.matched = 0
            self.latest_input = self.latest_match = None
            self.events.clear()
            self.start_mono = None
            self.elapsed = 0
            self.run_id = None
            self.board_info = None
            self.path = None
            self.log_size = 0
            self.session = secrets.token_hex(12)
            if self.library:
                self.library.positions = {label: 0 for label in range(22)}
            self.stop_event = threading.Event()
            self.thread = threading.Thread(target=self._run, args=(port,), daemon=True)
            self.thread.start()

    def select(self, label):
        if type(label) is not int or not 0 <= label < 22:
            raise ValueError("请选择有效的运行状态")
        with self.lock:
            if self.status != "running":
                raise ValueError("请先开始演示")
            self.auto = False
            self.requested_label = label
            if self.source_kind == "live":
                self.simulator.select(label)

    def stop(self, wait=False):
        with self.lock:
            self.stop_event.set()
            if self.status in ACTIVE:
                self.status = "stopping"
        if wait and self.thread:
            self.thread.join(18)
            if self.thread.is_alive():
                raise ValueError("正在结束板端事务，请稍后重试")

    def export(self, session):
        with self.lock:
            if not self.path or session != self.session:
                raise ValueError("演示会话已切换，原记录保留在本机")
            return self.path.open("rb"), self.path.name, self.log_size

    def _record(self, log, event, **fields):
        raw = (json.dumps(dict(event=event, timestamp=utc_now(), **fields), ensure_ascii=False) + "\n").encode()
        with self.lock:
            log.write(raw)
            log.flush()
            self.log_size = log.tell()

    def _wait_wire(self, ack, sent_ns, serial_session):
        deadline = time.monotonic() + 2
        # Drain the already-published window on Stop; do not leave its RS485
        # correspondence incomplete just because the operator ended the run.
        while time.monotonic() < deadline:
            state = self.monitor.snapshot()
            if state["link"] != "connected" or state["session"] != serial_session:
                raise OSError("RS485 接收已断开或切换，演示已停止")
            for row in state["rows"]:
                if (row.get("result_valid") and row.get("sequence") == ack["sequence"]
                        and row["start_monotonic_ns"] >= sent_ns):
                    if row["registers"] != ack["registers"] or not row.get("npu_executed"):
                        raise OSError("RS485 返回字段与本次板端发布不一致")
                    return dict(sequence=row["sequence"], label=row["label"], label_name=row["label_name"],
                                response_hex=row["response_hex"], registers=row["registers"],
                                serial_record_id=row["id"], serial_session=serial_session,
                                end_to_end_ms=round((row["end_monotonic_ns"] - sent_ns) / 1e6, 3),
                                inference_us=row["inference_us"], received_at=row["received_at"])
            time.sleep(.01)
        raise OSError("2 秒内未收到本窗对应的 RS485 结果，演示已停止")

    def _run(self, port):
        opened_serial = False
        run_id = None
        log = None
        failure = None
        previous_prediction = None
        try:
            state = self.board.state()
            if (state.get("model") != "sfaug-r1" or state.get("sensor_rules") != {"enabled": False}
                    or not state.get("external_input", {}).get("supported")):
                raise OSError("板端需要支持网口波形输入的 sfaug-r1 纯 NPU 版本")
            if state["status"] in {"starting", "running", "finalizing"}:
                raise OSError("板端已有任务在运行，请先在原页面停止该任务")
            with self.lock:
                self.board_info = dict(model=state["model"], session=state["session"], rules=False)
            serial_state = self.monitor.snapshot()
            if serial_state["link"] != "connected":
                self.monitor.start(port, 100)
                opened_serial = True
                serial_state = self.monitor.snapshot()
            if serial_state["config"]["period_ms"] > 500:
                raise ValueError("演示请将 RS485 查询间隔设置为 50–500 ms")
            serial_session = serial_state["session"]
            if self.stop_event.is_set():
                return
            self.output.mkdir(parents=True, exist_ok=True)
            path = self.output / (self.session + ".jsonl")
            log = path.open("xb")
            with self.lock:
                self.path = path
            self._record(log, "start", session=self.session, serial_session=serial_session,
                         source_kind=self.source_kind,
                         library_sha256=self.library.manifest["sha256"] if self.source_kind == "library" else None,
                         simulator_session=self.simulator.snapshot()["session"] if self.source_kind == "live" else None,
                         automatic=self.auto)
            run_id = self.board.request("/api/stream/start", {})["run_id"]
            with self.lock:
                self.run_id = run_id
            deadline = time.monotonic() + 30
            while not self.stop_event.is_set():
                state = self.board.state()
                if state["run_id"] != run_id or state["status"] in {"error", "stopped", "completed"}:
                    raise OSError("板端输入会话未就绪：" + str(state.get("error")))
                if state["status"] == "running":
                    break
                if time.monotonic() > deadline:
                    raise OSError("板端 NPU 加载超时")
                self.stop_event.wait(.1)
            with self.lock:
                self.start_mono = time.monotonic()
                self.status = "stopping" if self.stop_event.is_set() else "running"
            previous_window = None
            for input_id in range(1, 10001):
                if self.stop_event.is_set():
                    break
                begin = time.monotonic()
                with self.lock:
                    elapsed = begin - self.start_mono
                    if self.auto:
                        if elapsed >= 21:
                            break
                        self.requested_label = next(y for t, y in reversed(STEPS) if elapsed >= t)
                    label = self.requested_label
                if self.source_kind == "live":
                    # Sample the most recent complete window. No queue can build
                    # behind the physical NPU/RS485 handshake.
                    item = None
                    while not self.stop_event.is_set():
                        if self.simulator.snapshot()["status"] != "running":
                            break
                        try:
                            item = self.simulator.window(previous_window)
                        except ValueError:
                            if self.simulator.snapshot()["status"] == "running":
                                raise
                            break
                        if item:
                            break
                        self.stop_event.wait(.01)
                    if not item:
                        break
                    raw, context = item
                    previous_window = context["source_index"]
                    label = context["source_label"]
                    with self.lock:
                        self.requested_label = label
                else:
                    raw, context = self.library.next(label)
                context.update(input_id=input_id, bytes=len(raw), elapsed_s=round(elapsed, 3))
                self._record(log, "send", **{k: v for k, v in context.items() if k != "waveform"})
                sent_ns = time.monotonic_ns()
                with self.lock:
                    self.latest_input = context
                body = dict(
                    run_id=run_id, input_id=input_id, sha256=context["sha256"],
                    samples_b64=base64.b64encode(raw).decode("ascii"))
                # Old board releases accept the original four fields. New ones
                # advertise display-only units/time; the class stays on the PC.
                if self.source_kind == "live" and state.get("external_input", {}).get("display_metadata_version") == 1:
                    body["display"] = dict(scale_a=context["physical_scale_a"],
                                           sample_rate_hz=20000, sim_time_s=context["sim_time_s"])
                ack = self.board.request("/api/stream/window", body, timeout=15)
                if (ack.get("run_id") != run_id or ack.get("input_id") != input_id
                        or ack.get("sha256") != context["sha256"] or ack.get("bytes") != 6000
                        or ack.get("model") != "sfaug-r1" or not ack.get("sample_transport", {}).get("physical")
                        or not ack.get("axi", {}).get("physical")):
                    raise OSError("板端输入回执或物理 PL / AXI 校验失败")
                self._record(log, "board_ack", **ack)
                with self.lock:
                    self.sent += 1
                received = self._wait_wire(ack, sent_ns, serial_session)
                received.update(input_id=input_id, source_label=label, matches_reference=received["label"] == label)
                self._record(log, "rs485_match", **received)
                with self.lock:
                    self.matched += 1
                    self.latest_match = received
                    if received["label"] != previous_prediction:
                        kind = "监测建立" if previous_prediction is None else "恢复正常" if received["label"] == 0 else "故障识别"
                        self.events.append(dict(kind=kind, elapsed_s=round(time.monotonic() - self.start_mono, 2), **received))
                        previous_prediction = received["label"]
                self.stop_event.wait(max(0, .15 - (time.monotonic() - begin)))
        except Exception as error:
            failure = str(error)
        finally:
            with self.lock:
                self.status = "stopping"
                self.elapsed = round(time.monotonic() - self.start_mono, 1) if self.start_mono else 0
            if run_id:
                try:
                    self.board.request("/api/stream/stop", {"run_id": run_id})
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        state = self.board.state()
                        if state["run_id"] != run_id or state["status"] not in {"starting", "running", "finalizing"}:
                            break
                        time.sleep(.05)
                    else:
                        raise OSError("板端仍在保存，请查看板端运行状态")
                except Exception as error:
                    failure = failure or str(error)
            if opened_serial:
                try:
                    self.monitor.stop()
                except Exception as error:
                    failure = failure or str(error)
            if log:
                try:
                    self._record(log, "complete", error=failure, sent=self.sent, matched=self.matched,
                                 run_id=run_id, elapsed_s=self.elapsed)
                except OSError as error:
                    failure = failure or str(error)
                finally:
                    log.close()
            with self.lock:
                self.status = "error" if failure else "stopped"
                self.error = failure or ""
