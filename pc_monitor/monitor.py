"""Bounded serial polling and durable wire records for the local PC frontend."""
from collections import deque
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading
import time
import uuid

from protocol.modbus import crc16, decode_response, read_request
from tools.rs485_capture import read_response

REQUEST = read_request()
LABELS = ["正常运行"] + [f"{phase}{i} 开关开路" for phase in ("Ta", "Tb", "Tc") for i in range(1, 5)]
LABELS += [f"{phase} 相传感器{kind}" for kind in ("断线", "卡死", "偏移") for phase in "ABC"]
CODES = ["Normal"] + [f"{phase}{i} OS" for phase in ("Ta", "Tb", "Tc") for i in range(1, 5)]
CODES += [f"Sensor {phase} {kind}" for kind in ("DF", "SF", "OF") for phase in "ABC"]
ERRORS = {0: "无错误", 1: "NPU 错误", 2: "存储错误", 3: "AXI 错误", 4: "输入错误", 5: "结果发布已超时"}
CSV_FIELDS = ["id", "timestamp", "port", "request_hex", "response_hex", "stale_hex", "status",
              "crc_ok", "wire_ok", "abi_ok", "result_valid", "abi", "flags", "label", "label_name",
              "sequence", "confidence_q", "inference_us", "application_error", "roundtrip_ms", "error"]


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def decode(raw):
    """Decode real bytes, separating a valid packet from a valid diagnosis."""
    record = dict(response_hex=raw.hex(" ").upper(), byte_count=len(raw), crc_ok=None,
                  wire_ok=False, abi_ok=False, result_valid=False, status="timeout" if not raw else "invalid",
                  error="等待应答超时" if not raw else "")
    if len(raw) >= 4:
        record.update(crc_ok=crc16(raw[:-2]) == int.from_bytes(raw[-2:], "little"),
                      crc_received=f"{int.from_bytes(raw[-2:], 'little'):04X}",
                      crc_calculated=f"{crc16(raw[:-2]):04X}")
    if not raw:
        return record
    try:
        regs = list(decode_response(raw))
    except ValueError as error:
        record["error"] = str(error)
        if record["crc_ok"] is False:
            record.update(status="crc_error", error="CRC 校验失败")
        elif len(raw) == 5 and raw[0] == 1 and raw[1] == 0x83 and record["crc_ok"]:
            record.update(status="exception", exception_code=raw[2], error=f"Modbus 异常码 {raw[2]:02X}")
        return record
    record.update(wire_ok=True, registers=regs, abi=regs[0], flags=regs[1], label=regs[2],
                  confidence_q=regs[3], sequence=(regs[4] << 16) | regs[5],
                  inference_us=(regs[6] << 16) | regs[7], application_error=(regs[8] << 16) | regs[9])
    # An unsupported ABI must never be silently interpreted as a current diagnosis.
    if regs[0] != 1 or regs[1] & ~7 or regs[3] > 10000:
        record.update(status="invalid", error="不支持的 ABI、标志位或分数范围")
        return record
    valid = bool(regs[1] & 1)
    if (valid and (regs[2] >= len(LABELS) or record["application_error"])) or (not valid and regs[2] != 0xFFFF):
        record.update(status="invalid", error="结果有效位与类别 / 错误码不一致")
        return record
    record.update(abi_ok=True, result_valid=valid, npu_executed=bool(regs[1] & 2),
                  simulated=bool(regs[1] & 4), status="ok" if valid else "idle",
                  label_name=LABELS[regs[2]] if valid else "暂无有效诊断",
                  label_code=CODES[regs[2]] if valid else "INVALID",
                  application_error_name=ERRORS.get(record["application_error"], "未知应用错误"))
    if record["application_error"]:
        record["label_name"] = record["application_error_name"]
    return record


def detected_ports():
    from serial.tools import list_ports
    return sorted([dict(device=p.device, description=p.description) for p in list_ports.comports()],
                  key=lambda p: (p["device"] != "COM22", p["device"]))


def open_serial(name):
    import serial
    port = serial.Serial(port=None, baudrate=115200, bytesize=8, parity=serial.PARITY_EVEN,
                         stopbits=serial.STOPBITS_ONE, timeout=0.25, write_timeout=0.5)
    port.dtr = False
    port.rts = False
    port.port = name
    port.open()
    return port


class Monitor:
    def __init__(self, output, serial_factory=open_serial, ports_provider=detected_ports):
        self.output = Path(output)
        self.serial_factory = serial_factory
        self.ports_provider = ports_provider
        self.lock = threading.RLock()
        self.control = threading.Lock()
        self.thread = None
        self.stop_event = threading.Event()
        self.rows = deque(maxlen=600)
        self.link = "disconnected"
        self.error = ""
        self.session = None
        self.config = None
        self.started_at = None
        self.ended_at = None
        self.latest = None
        self.last_good_mono = None
        self.last_sequence = None
        self.files = {}
        self.counts = self.empty_counts()

    @staticmethod
    def empty_counts():
        return dict(queries=0, responses=0, valid_packets=0, active_results=0, updates=0,
                    crc_errors=0, timeouts=0, protocol_errors=0, stale_bytes=0)

    def start(self, port, period_ms=100):
        if not isinstance(port, str) or not re.fullmatch(r"COM\d+", port, re.IGNORECASE):
            raise ValueError("请选择有效的 Windows COM 口")
        if type(period_ms) is not int or not 50 <= period_ms <= 5000:
            raise ValueError("查询间隔应为 50–5000 ms")
        ports = self.ports_provider()
        if port.upper() not in {p["device"].upper() for p in ports}:
            raise ValueError("未检测到所选串口，请插入适配器并刷新串口列表")
        with self.control:
            if self.thread and self.thread.is_alive():
                raise ValueError("串口正在接收，请先断开")
            serial_port = self.serial_factory(port)
            try:
                self.output.mkdir(parents=True, exist_ok=True)
                session = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
                files = {fmt: self.output / f"{session}.{fmt}" for fmt in ("jsonl", "csv")}
                log = files["jsonl"].open("x", encoding="utf-8")
                try:
                    csv_file = files["csv"].open("x", encoding="utf-8-sig", newline="")
                except Exception:
                    log.close()
                    raise
            except Exception:
                serial_port.close()
                raise
            with self.lock:
                self.session, self.files = session, files
                self.config = dict(port=port, baud=115200, format="8E1", slave=1, function=3,
                                   start=0, count=10, period_ms=period_ms, timeout_ms=250)
                self.rows.clear()
                self.counts = self.empty_counts()
                self.latest = self.last_sequence = self.last_good_mono = None
                self.started_at, self.ended_at = utc_now(), None
                self.link, self.error = "connected", ""
                self.stop_event = threading.Event()
                self.thread = threading.Thread(target=self._poll, args=(serial_port, log, csv_file), daemon=True)
                self.thread.start()

    def stop(self):
        with self.control:
            self.stop_event.set()
            if self.thread:
                self.thread.join(timeout=2)
                if self.thread.is_alive():
                    raise ValueError("正在结束串口事务，请稍后重试断开")

    def _poll(self, port, log, csv_file):
        writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS, extrasaction="ignore")
        reason = "disconnected"
        pending = None
        try:
            writer.writeheader()
            log.write(json.dumps(dict(event="start", session=self.session, timestamp=self.started_at,
                                      config=self.config), ensure_ascii=False) + "\n")
            log.flush()
            csv_file.flush()
            while not self.stop_event.is_set():
                begin = time.monotonic_ns()
                stale = port.read(min(port.in_waiting, 4096)) if port.in_waiting else b""
                pending = dict(timestamp=utc_now(), request_hex=REQUEST.hex(" ").upper(), stale_hex=stale.hex(" ").upper())
                if port.write(REQUEST) != len(REQUEST):
                    raise OSError("串口请求未完整发出")
                raw = read_response(port, 0.25)
                end = time.monotonic_ns()
                with self.lock:
                    row = dict(id=self.counts["queries"] + 1, session=self.session, port=self.config["port"],
                               received_at=utc_now(), start_monotonic_ns=begin, end_monotonic_ns=end,
                               roundtrip_ms=round((end - begin) / 1e6, 3), **pending, **decode(raw))
                    # Flush evidence before exposing the same complete record to the browser.
                    log.write(json.dumps(dict(event="query", **row), ensure_ascii=False) + "\n")
                    writer.writerow(row)
                    log.flush()
                    csv_file.flush()
                    self._accept(row)
                pending = None
                delay = max(0.005, self.config["period_ms"] / 1000 - (end - begin) / 1e9)
                self.stop_event.wait(delay)
        except Exception as error:
            reason = "error"
            with self.lock:
                self.error = str(error)
            try:
                log.write(json.dumps(dict(event="failure", timestamp=utc_now(), error=str(error),
                                          pending_request=pending), ensure_ascii=False) + "\n")
                log.flush()
            except OSError:
                pass
        finally:
            try:
                port.close()
            finally:
                with self.lock:
                    self.link, self.ended_at = reason, utc_now()
                try:
                    log.write(json.dumps(dict(event="complete", session=self.session, timestamp=self.ended_at,
                                              reason=reason, counts=self.counts), ensure_ascii=False) + "\n")
                finally:
                    log.close()
                    csv_file.close()

    def _accept(self, row):
        self.rows.append(row)
        self.latest = row
        c = self.counts
        c["queries"] += 1
        c["responses"] += int(row["byte_count"] > 0)
        c["valid_packets"] += int(row["abi_ok"])
        c["active_results"] += int(row["result_valid"])
        c["crc_errors"] += int(row["crc_ok"] is False)
        c["timeouts"] += int(row["status"] == "timeout")
        c["protocol_errors"] += int(row["status"] in ("invalid", "exception"))
        c["stale_bytes"] += len(row["stale_hex"].replace(" ", "")) // 2
        if row["abi_ok"]:
            self.last_good_mono = time.monotonic()
        if row["result_valid"] and row["sequence"] != self.last_sequence:
            c["updates"] += 1
            self.last_sequence = row["sequence"]

    def snapshot(self, after=0, session=None):
        with self.lock:
            after = after if session == self.session else 0
            rows = [r for r in self.rows if r["id"] > after][-200:]
            return dict(identity="powerscope-rs485-monitor", version=1, link=self.link, error=self.error,
                        session=self.session, config=self.config, started_at=self.started_at, ended_at=self.ended_at,
                        counts=dict(self.counts), latest=self.latest, rows=rows,
                        omitted_rows=max(0, rows[0]["id"] - after - 1) if rows else 0,
                        last_good_age_ms=round((time.monotonic() - self.last_good_mono) * 1000) if self.last_good_mono else None,
                        downloads={fmt: f"/api/export/{fmt}?session={self.session}" for fmt in self.files})

    def export(self, fmt, session):
        with self.lock:
            if fmt not in self.files or session != self.session:
                raise ValueError("该会话已切换，请刷新后导出当前会话；此前记录仍保存在电脑本地")
            f = self.files[fmt].open("rb")
            # Capture the complete-record boundary while the writer is locked.
            f.seek(0, 2)
            size = f.tell()
            f.seek(0)
            return f, self.files[fmt].name, size
