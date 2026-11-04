"""Continuous local NPC switching circuit and paced, bounded live windows.

The physical solver produces amperes and volts. Only the copy sent to the
diagnostic interface is jointly normalized. No prediction is generated here.
"""
import csv
import ctypes as ct
from collections import deque
import hashlib
import json
import math
from pathlib import Path
import secrets
import struct
import sys
import threading
import time

from pc_monitor.monitor import LABELS, utc_now

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "artifacts/live-sim-runtime"
CORE = ROOT / "pc_monitor/native/npc_core.c"
MODEL = "npc-switching-r1"
CONDITIONS = [
    dict(id=1, name="C1 · 100 V / 10 Ω", vdc=100, resistance=10, inductance=0),
    dict(id=2, name="C2 · 100 V / 20 Ω", vdc=100, resistance=20, inductance=0),
    dict(id=3, name="C3 · 100 V / 10 Ω / 1 mH", vdc=100, resistance=10, inductance=.001),
    dict(id=4, name="C4 · 100 V / 5 Ω / 2 mH", vdc=100, resistance=5, inductance=.002),
]
SEQUENCE = [(0, 0), (3, 2), (6, 0), (9, 16), (12, 0), (15, 21), (18, 0)]


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def runtime_status():
    try:
        manifest = json.loads((RUNTIME / "build.json").read_text(encoding="utf-8-sig"))
        if (manifest["source_sha256"] != sha256(CORE)
                or manifest["dll_sha256"] != sha256(RUNTIME / "npc_core.dll")):
            return False, "仿真核心版本已更新，请运行 tools/build_live_sim.ps1"
        return True, ""
    except (OSError, ValueError, KeyError):
        return False, "请先运行 tools/build_live_sim.ps1 编译本机仿真核心"


class NativePlant:
    def __init__(self, condition):
        valid, message = runtime_status()
        if not valid:
            raise ValueError(message)
        self.dll = ct.CDLL(str(RUNTIME / "npc_core.dll"))
        self.dll.npc_create.argtypes = [ct.c_double] * 3
        self.dll.npc_create.restype = ct.c_void_p
        self.dll.npc_destroy.argtypes = [ct.c_void_p]
        self.dll.npc_destroy.restype = None
        self.dll.npc_fault.argtypes = [ct.c_void_p, ct.c_int]
        self.dll.npc_advance.argtypes = [ct.c_void_p] + [ct.POINTER(ct.c_float)] * 3 + [ct.c_int]
        self.dll.npc_info.argtypes = [ct.c_void_p, ct.POINTER(ct.c_double)]
        self.handle = self.dll.npc_create(condition["vdc"], condition["resistance"], condition["inductance"])
        if not self.handle:
            raise ValueError("电路参数超出模型范围")
        self.buffers = None
        self.count = 0

    def close(self):
        if self.handle:
            self.dll.npc_destroy(self.handle)
            self.handle = None

    def fault(self, label):
        if type(label) is not int or not 0 <= label < 22:
            raise ValueError("请选择有效的故障位置")
        if self.dll.npc_fault(self.handle, label):
            raise ValueError("故障参数设置失败")

    def advance(self, count=500):
        if type(count) is not int or not 1 <= count <= 20000:
            raise ValueError("采样数超出范围")
        if not self.handle:
            raise ValueError("仿真已关闭")
        if count != self.count:
            self.buffers = [(ct.c_float * (3 * count))() for _ in range(3)]
            self.count = count
        if self.dll.npc_advance(self.handle, *self.buffers, count):
            raise ValueError("电路求解数值异常，已停止此次运行")
        info = (ct.c_double * 18)()
        self.dll.npc_info(self.handle, info)
        waves = [[[float(buffer[k * count + i]) for i in range(count)] for k in range(3)]
                 for buffer in self.buffers]
        return dict(measured=waves[0], actual=waves[1], voltage=waves[2],
                    solver_time_s=info[0], nominal_peak_a=info[1],
                    modulation=list(info[2:5]), gates=list(info[5:17]), power_w=info[17])


def normalized_window(waves):
    values = [value for phase in waves for value in phase]
    if len(values) != 1500 or not all(math.isfinite(v) for v in values):
        raise ValueError("需要三相各 500 点有限数值")
    scale = max(max(abs(v) for v in values), 1e-9)
    return struct.pack("<1500f", *(v / scale for v in values)), scale


class LiveSimulation:
    def __init__(self, output=None):
        self.output = Path(output or ROOT / "artifacts/live-sim-20261103/sessions")
        self.lock = threading.RLock()
        self.status = "idle"
        self.error = ""
        self.thread = None
        self.stop_event = threading.Event()
        self.available, self.runtime_error = runtime_status()
        self.session = None
        self.condition = CONDITIONS[0]
        self.automatic = False
        self.requested_label = 0
        self.label = 0
        self.latest = None
        self.events = deque(maxlen=80)
        self.history = deque(maxlen=1200)
        self.path = None
        self.log_size = 0
        self.wall_start = None
        self.elapsed = 0
        self.schedule_resyncs = 0

    def start(self, condition=1, automatic=False):
        if type(condition) is not int or condition not in range(1, 5) or type(automatic) is not bool:
            raise ValueError("请选择有效工况及运行方式")
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError("仿真正在运行")
            self.available, self.runtime_error = runtime_status()
            if not self.available:
                raise ValueError(self.runtime_error)
            self.condition = dict(CONDITIONS[condition - 1])
            self.automatic = automatic
            self.requested_label = self.label = 0
            self.latest = None
            self.events.clear(); self.history.clear()
            self.session = secrets.token_hex(12)
            self.path = None; self.log_size = 0
            self.elapsed = 0; self.wall_start = None
            self.schedule_resyncs = 0
            self.error = ""; self.status = "starting"
            self.stop_event = threading.Event()
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def select(self, label):
        if type(label) is not int or not 0 <= label < 22:
            raise ValueError("请选择有效的故障类型")
        with self.lock:
            if self.status != "running":
                raise ValueError("请先启动仿真")
            self.automatic = False
            self.requested_label = label

    def stop(self, wait=False):
        with self.lock:
            if self.status in {"starting", "running"}:
                self.status = "stopping"
            self.stop_event.set()
        if wait and self.thread:
            self.thread.join(5)
            if self.thread.is_alive():
                raise ValueError("仿真正在保存本次记录")

    def snapshot(self):
        with self.lock:
            return dict(status=self.status, error=self.error, available=self.available,
                        runtime_error=self.runtime_error, model=MODEL, session=self.session,
                        condition=self.condition, conditions=CONDITIONS, labels=LABELS,
                        automatic=self.automatic, label=self.label, requested_label=self.requested_label,
                        latest=self.latest, elapsed_s=self.elapsed,
                        schedule_resyncs=self.schedule_resyncs,
                        events=list(self.events), history=list(self.history),
                        sample_rate_hz=20000, solver_step_us=1, pwm_hz=10000,
                        export=f"/api/simulation/export?session={self.session}" if self.path else None)

    def export(self, session):
        with self.lock:
            if not self.path or session != self.session:
                raise ValueError("仿真会话已切换")
            return self.path.open("rb"), self.path.name, self.log_size

    def window(self, previous=None):
        with self.lock:
            if self.status != "running" or not self.latest:
                raise ValueError("请先启动在线仿真")
            window = self.latest
            if window["window_id"] == previous:
                return None
            raw, scale = normalized_window(window["measured"])
            return raw, dict(source_kind=MODEL, source_label=window["label"],
                             source_name=LABELS[window["label"]], source_index=window["window_id"],
                             simulator_session=self.session, sim_time_s=window["sim_time_s"],
                             physical_scale_a=scale, sha256=hashlib.sha256(raw).hexdigest(),
                             waveform=[[v / scale for v in phase[::5]] for phase in window["measured"]])

    def _run(self):
        plant = None
        log = None
        timer = None
        try:
            if sys.platform == "win32":
                timer = ct.WinDLL("winmm")
                if timer.timeBeginPeriod(1) != 0:
                    timer = None
            plant = NativePlant(self.condition)
            # Advance the continuous healthy circuit to a settled operating point.
            # Warm-up is identified separately from the paced session timeline.
            for _ in range(80):
                if self.stop_event.is_set():
                    return
                plant.advance()
            self.output.mkdir(parents=True, exist_ok=True)
            path = self.output / (self.session + ".csv")
            log = path.open("x", encoding="utf-8-sig", newline="")
            writer = csv.writer(log)
            writer.writerow(["time_s", "fault_input", "ia_rms_A", "ib_rms_A", "ic_rms_A",
                             "load_power_W", "compute_ms", "wall_lag_ms", "window_sha256",
                             "schedule_adjust_ms", "wall_elapsed_s"])
            log.flush()
            with self.lock:
                self.wall_start = time.monotonic()
                pace_anchor = self.wall_start
                self.path = path
                self.status = "running"
                self.events.append(dict(kind="开始运行", label=0, time_s=0, name=LABELS[0]))
            n = 0
            while not self.stop_event.is_set():
                with self.lock:
                    if self.automatic:
                        if n * .025 >= 21:
                            break
                        self.requested_label = next(y for t, y in reversed(SEQUENCE) if n * .025 >= t)
                    label = self.requested_label
                    if label != self.label:
                        plant.fault(label)
                        self.label = label
                        self.events.append(dict(kind="恢复正常" if label == 0 else "故障注入",
                                                label=label, time_s=round(n * .025, 3), name=LABELS[label]))
                started = time.perf_counter()
                batch = plant.advance()
                compute_ms = (time.perf_counter() - started) * 1000
                n += 1
                rms = [math.sqrt(sum(v * v for v in phase[-400:]) / 400) for phase in batch["measured"]]
                power = batch["power_w"]
                raw, scale = normalized_window(batch["measured"])
                sim_time = round(n * .025, 6)
                target = pace_anchor + sim_time
                if self.stop_event.wait(max(0, target - time.monotonic())):
                    break
                lag_ms = max(0, (time.monotonic() - target) * 1000)
                schedule_adjust_ms = 0
                if lag_ms > 250:
                    # Desktop scheduling/suspend is not a hard real-time clock.
                    # Preserve circuit states, record the gap, and rebase pacing
                    # instead of accumulating old windows in a catch-up queue.
                    schedule_adjust_ms = lag_ms
                    pace_anchor += lag_ms / 1000
                    with self.lock:
                        self.schedule_resyncs += 1
                        self.events.append(dict(kind="调度同步", label=label, time_s=sim_time,
                                                name=f"间隔 {lag_ms/1000:.2f} s"))
                wall_elapsed = time.monotonic() - self.wall_start
                batch.update(window_id=n, label=label, sim_time_s=sim_time,
                             compute_ms=round(compute_ms, 3), speed=round(25/max(compute_ms,.001), 2),
                             lag_ms=round(lag_ms, 3), rms_a=rms, power_w=power,
                             wall_elapsed_s=round(wall_elapsed, 3),
                             pace_ratio=round(sim_time/max(wall_elapsed,.001),3),
                             schedule_adjust_ms=round(schedule_adjust_ms,3),
                             normalization_scale_a=scale, sha256=hashlib.sha256(raw).hexdigest(),
                             timestamp=utc_now())
                writer.writerow([sim_time,label,*rms,power,compute_ms,lag_ms,batch["sha256"],
                                 schedule_adjust_ms,wall_elapsed])
                log.flush()
                with self.lock:
                    self.latest = batch
                    self.elapsed = sim_time
                    self.log_size = log.tell()
                    self.history.append(dict(time_s=sim_time, rms_a=rms, label=label))
                if sim_time >= 600:
                    break
        except Exception as error:
            with self.lock:
                self.error = str(error)
        finally:
            if plant:
                plant.close()
            if log:
                log.close()
            if timer:
                timer.timeEndPeriod(1)
            with self.lock:
                self.status = "error" if self.error else "stopped"
