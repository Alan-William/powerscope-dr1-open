"""Authenticated loopback receiver for the original live Simulink plant.

MATLAB owns model start and the tunable fault control. This receiver records
the actual model outputs and exposes the same latest-window interface as the
native demo, allowing the existing physical board/RS485 handshake to be reused.
"""
from collections import deque
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import socket
import struct
import threading
import time

from pc_monitor.monitor import LABELS, utc_now
from pc_monitor.simulator import CONDITIONS, ROOT, normalized_window

MODEL = "npc-simulink-live-r1"
HEADER = struct.Struct("<4sI16sIIII7d")
SAMPLES = struct.Struct("<4500f")
COMMAND = struct.Struct("<4sI16sIII")
CONFIG = struct.Struct("<4sI16sIIdddI")
ACTIVE = {"armed", "starting", "running", "stopping"}


def verify_build(runtime):
    build=json.loads((runtime/"build.json").read_text(encoding="utf-8-sig"))
    targets={runtime/"powerscope_npc_live.slx":build["model_sha256"],
             runtime/"powerscope_bridge.mexw64":build["bridge_sha256"]}
    for filename in ("powerscope_bridge.c","powerscope_sensor.m","powerscope_build.m"):
        targets[ROOT/"matlab"/filename]=build["implementation"][filename.replace('.','_')]
    for filename in ("npc_diag.slx","ctrl_npc.m","params_npc.m","sensor_fault.m"):
        targets[runtime/"research-copy/sim"/filename]=build["source"][filename.replace('.','_')]
    for path,expected in targets.items():
        if hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
            raise ValueError("Simulink 模型或通信模块版本已变化，请重新构建："+path.name)
    return build


def decode_packet(raw, token):
    if len(raw) < HEADER.size:
        raise ValueError("Truncated Simulink packet")
    magic, version, nonce, seq, label, command, automatic, *values = HEADER.unpack_from(raw)
    if version != 1 or nonce != token or magic not in {b"PSW1", b"PSR1", b"PSL1", b"PSE1"}:
        raise ValueError("Invalid Simulink session or protocol")
    if label > 21 or automatic > 1 or not all(math.isfinite(v) for v in values):
        raise ValueError("Invalid Simulink metadata")
    frame = dict(kind=magic.decode(), window_id=seq, label=label, command_id=command,
                 automatic=bool(automatic))
    frame.update(zip(("sim_time_s", "wall_elapsed_s", "compute_ms", "lag_ms",
                      "nominal_peak_a", "power_w", "applied_at_s"), values))
    if magic == b"PSL1":
        if len(raw) != HEADER.size + SAMPLES.size or seq < 1:
            raise ValueError("Invalid Simulink window length")
        samples = SAMPLES.unpack_from(raw, HEADER.size)
        if not all(math.isfinite(v) and abs(v) < 10000 for v in samples):
            raise ValueError("Invalid Simulink samples")
        for offset, name in enumerate(("measured", "actual", "voltage")):
            frame[name] = [list(samples[offset*1500 + phase*500:offset*1500 + (phase+1)*500])
                           for phase in range(3)]
        if abs(frame["sim_time_s"] - seq*.025) > 1e-6:
            raise ValueError("Simulink window clock mismatch")
    elif len(raw) != HEADER.size:
        raise ValueError("Unexpected Simulink status payload")
    return frame


class SimulinkSimulation:
    def __init__(self, output=None, runtime=None):
        self.output = Path(output or ROOT / "artifacts/simulink-live-20261103/sessions")
        self.runtime = Path(runtime or "E:/fpga_workflow_runs/powerscope-simulink-runtime")
        self.lock = threading.RLock()
        self.status = "idle"
        self.stage = "MATLAB / Simulink 控制台"
        self.error = ""
        self.latest = None
        self.session = None
        self.token = None
        self.condition = CONDITIONS[0]
        self.automatic = False
        self.requested_label = self.label = 0
        self.elapsed = 0
        self.events = deque(maxlen=80)
        self.history = deque(maxlen=1200)
        self.path = None
        self.log_size = 0
        self.thread = None
        self.sock = None
        self.peer = None
        self.stop_event = threading.Event()
        self.command_id = 0
        self.pending_command = None
        self.stop_at = None
        self.dropped_windows = 0
        self.invalid_packets = 0
        self.finished = None

    def start(self, condition=1, automatic=False):
        raise ValueError("请在 MATLAB 演示控制台启动 Simulink")

    def arm(self, condition=1, automatic=False, duration_s=None, warmup_s=2.0, pace=True):
        if type(condition) is not int or condition not in range(1,5) or type(automatic) is not bool:
            raise ValueError("请选择有效工况和运行方式")
        duration = 21.0 if automatic else 600.0
        if duration_s is not None:
            if type(duration_s) not in (float,int) or not .05 <= duration_s <= 600:
                raise ValueError("仿真时长超出范围")
            duration = float(duration_s)
        if type(warmup_s) not in (float,int) or not 0 <= warmup_s <= 10 or type(pace) is not bool:
            raise ValueError("无效仿真节奏参数")
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError("当前仿真记录仍在运行")
            build_path = self.runtime / "build.json"
            if not build_path.is_file():
                raise ValueError("请先运行 Simulink 演示启动器")
            try: build=verify_build(self.runtime)
            except (OSError,KeyError,TypeError) as error:
                raise ValueError("Simulink 构建记录不完整，请重新构建") from error
            self.condition = dict(CONDITIONS[condition-1])
            self.automatic = automatic
            self.requested_label = self.label = 0
            self.latest = None; self.elapsed = 0
            self.events.clear(); self.history.clear()
            self.session = secrets.token_hex(16); self.token = bytes.fromhex(self.session)
            self.path = None; self.log_size = 0
            self.error = ""; self.status = "starting"; self.stage = "Simulink 编译与初始化"
            self.peer = None; self.command_id = 0; self.pending_command = None
            self.stop_at = None; self.finished = None
            self.dropped_windows = self.invalid_packets = 0
            self.stop_event = threading.Event()
            self.output.mkdir(parents=True,exist_ok=True)
            self.sock = socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,4*1024*1024)
            self.sock.bind(("127.0.0.1",0)); self.sock.settimeout(.15)
            port = self.sock.getsockname()[1]
            c = self.condition
            w = 2*math.pi*50
            impedance = complex(.05,w*.002) + 1/(1/complex(c["resistance"],w*c["inductance"]) + complex(0,w*.00005))
            nominal_peak = .8*(c["vdc"]/2)/abs(impedance)
            config = self.runtime / "session.bin"
            tmp = self.runtime / (self.session + ".config.tmp")
            tmp.write_bytes(CONFIG.pack(b"PSCF",1,self.token,port,int(automatic),float(warmup_s),duration,nominal_peak,int(pace)))
            os.replace(tmp,config)
            metadata = dict(session=self.session,model=MODEL,condition=c,created_at=utc_now(),
                            duration_s=duration,warmup_s=warmup_s,pace=pace,build=build,
                            board_hardware_tested=False,packet_schema="PSL1",
                            source="Original Simulink NPC plant, Accelerator mode")
            (self.output/(self.session+".meta.json")).write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding="utf-8")
            self.thread = threading.Thread(target=self._receive,daemon=True)
            self.thread.start()
            return dict(session=self.session,udp_port=port,config_path=config.as_posix(),
                        duration_s=duration,warmup_s=warmup_s)

    def _send_command(self, op, label=0):
        self.command_id += 1
        self.pending_command = COMMAND.pack(b"PSC1",1,self.token,self.command_id,op,label)
        if self.peer:
            self.sock.sendto(self.pending_command,self.peer)

    def select(self,label):
        if type(label) is not int or not 0 <= label <= 21:
            raise ValueError("请选择有效故障类型")
        with self.lock:
            if self.status != "running":
                raise ValueError("请先在 MATLAB 启动仿真")
            self.automatic=False; self.requested_label=label
            self._send_command(1,label)

    def stop(self,wait=False):
        with self.lock:
            if self.status in ACTIVE:
                self.status="stopping"; self.stage="正在保存仿真记录"
                if self.stop_at is None:
                    self.stop_at=time.monotonic()
                    self._send_command(2)
        if wait and self.thread:
            self.thread.join(6)
            if self.thread.is_alive():
                self.stop_event.set(); self.thread.join(1)

    def model_failed(self,session,message):
        with self.lock:
            if session != self.session:
                raise ValueError("仿真会话已切换")
            self.error=str(message)[:2000]; self.stop_event.set()

    def snapshot(self):
        with self.lock:
            return dict(status=self.status,stage=self.stage,error=self.error,available=True,runtime_error="",
                        model=MODEL,engine="simulink",engine_title="MATLAB / Simulink · 原 NPC 模型",
                        session=self.session,condition=self.condition,conditions=CONDITIONS,labels=LABELS,
                        automatic=self.automatic,label=self.label,requested_label=self.requested_label,
                        latest=self.latest,elapsed_s=self.elapsed,schedule_resyncs=0,
                        dropped_windows=self.dropped_windows,invalid_packets=self.invalid_packets,
                        events=list(self.events),history=list(self.history),sample_rate_hz=20000,
                        solver_step_us=1,pwm_hz=10000,
                        export=f"/api/simulation/export?session={self.session}" if self.path else None)

    def window(self,previous=None):
        with self.lock:
            if self.status!="running" or not self.latest:
                raise ValueError("Simulink 当前没有运行中的电流窗口")
            w=self.latest
            if w["window_id"]==previous: return None
            raw,scale=normalized_window(w["measured"])
            return raw,dict(source_kind=MODEL,source_label=w["label"],source_name=LABELS[w["label"]],
                source_index=w["window_id"],simulator_session=self.session,sim_time_s=w["sim_time_s"],
                physical_scale_a=scale,sha256=hashlib.sha256(raw).hexdigest(),
                waveform=[[v/scale for v in p[::5]] for p in w["measured"]])

    def export(self,session):
        with self.lock:
            if not self.path or session!=self.session: raise ValueError("仿真会话已切换")
            return self.path.open("rb"),self.path.name,self.log_size

    def _receive(self):
        began=last=time.monotonic(); previous=0
        try:
            path=self.output/(self.session+".csv")
            with path.open("x",encoding="utf-8-sig",newline="") as csv_file, \
                    (self.output/(self.session+".windows.bin")).open("xb") as raw_file:
                writer=csv.writer(csv_file)
                writer.writerow(["time_s","fault_input","ia_rms_A","ib_rms_A","ic_rms_A", "load_power_W",
                                 "compute_ms","wall_lag_ms","window_sha256","wall_elapsed_s","window_id","applied_at_s"])
                csv_file.flush()
                with self.lock: self.path=path; self.log_size=csv_file.tell()
                while not self.stop_event.is_set():
                    try: raw,peer=self.sock.recvfrom(65535)
                    except socket.timeout:
                        now=time.monotonic()
                        if self.stop_at is not None and now-self.stop_at>5: break
                        if now-last>(180 if self.status=="starting" else 5):
                            raise ValueError("Simulink 数据流停止更新，请检查模型状态")
                        if self.peer and self.pending_command: self.sock.sendto(self.pending_command,self.peer)
                        continue
                    if peer[0]!="127.0.0.1" or (self.peer and peer!=self.peer):
                        self.invalid_packets+=1; continue
                    try: frame=decode_packet(raw,self.token)
                    except ValueError:
                        self.invalid_packets+=1; continue
                    last=time.monotonic(); self.peer=peer
                    if self.pending_command:
                        if frame["command_id"]>=self.command_id: self.pending_command=None
                        else: self.sock.sendto(self.pending_command,peer)
                    if frame["kind"]=="PSW1":
                        with self.lock: self.stage="电路预热 · 2 s 仿真时间"
                        continue
                    if frame["kind"]=="PSR1":
                        with self.lock:
                            if self.status!="stopping": self.status="running"; self.stage="Simulink 在线运行"
                            self.events.append(dict(kind="开始运行",label=0,time_s=0,name=LABELS[0]))
                        continue
                    if frame["kind"]=="PSE1":
                        self.finished=frame; break
                    seq=frame["window_id"]
                    if seq<=previous: continue
                    if seq!=previous+1: self.dropped_windows+=seq-previous-1
                    previous=seq
                    rms=[math.sqrt(sum(v*v for v in row[-400:])/400) for row in frame["measured"]]
                    normalized,scale=normalized_window(frame["measured"])
                    frame.update(rms_a=rms,speed=25/max(frame["compute_ms"],.001),
                                 pace_ratio=frame["sim_time_s"]/max(frame["wall_elapsed_s"],.001),
                                 normalization_scale_a=scale,sha256=hashlib.sha256(normalized).hexdigest(),
                                 timestamp=utc_now(),schedule_adjust_ms=0)
                    writer.writerow([frame["sim_time_s"],frame["label"],*rms,frame["power_w"],frame["compute_ms"],
                        frame["lag_ms"],frame["sha256"],frame["wall_elapsed_s"],seq,frame["applied_at_s"]])
                    csv_file.flush(); raw_file.write(raw); raw_file.flush()
                    with self.lock:
                        if frame["label"]!=self.label:
                            self.label=frame["label"]
                            self.events.append(dict(kind="恢复正常" if self.label==0 else "故障注入",label=self.label,
                                time_s=frame["applied_at_s"],name=LABELS[self.label]))
                        self.requested_label=self.label; self.automatic=frame["automatic"]
                        if self.status!="stopping": self.status="running"
                        self.latest=frame; self.elapsed=frame["sim_time_s"]; self.log_size=csv_file.tell()
                        self.history.append(dict(time_s=self.elapsed,rms_a=rms,label=self.label))
        except Exception as error:
            with self.lock: self.error=str(error)
        finally:
            if self.sock: self.sock.close()
            with self.lock:
                self.status="error" if self.error else "stopped"
                self.stage="运行记录已保存" if not self.error else self.error
            result=dict(session=self.session,ended_at=utc_now(),status=self.status,error=self.error,
                        windows=previous,elapsed_s=self.elapsed,dropped_windows=self.dropped_windows,
                        invalid_packets=self.invalid_packets,received_end_packet=self.finished is not None)
            (self.output/(self.session+".result.json")).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
