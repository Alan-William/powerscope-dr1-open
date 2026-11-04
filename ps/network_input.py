"""One outstanding Ethernet waveform, with an expiring input lease. Python 3.8."""
import base64
import hashlib
import math
import struct
import threading
import time


def display_metadata(data):
    """Display-only units/time. Never used by PL conversion or inference."""
    if "display" not in data:
        return None
    value = data["display"]
    if not isinstance(value, dict) or set(value) != {"scale_a", "sample_rate_hz", "sim_time_s"}:
        raise ValueError("Display metadata accepts only scale_a, sample_rate_hz and sim_time_s")
    for key, lower, upper in (("scale_a", 0, 1e6), ("sim_time_s", 0, 1e9)):
        number = value[key]
        if type(number) not in (int, float) or not math.isfinite(number) or not lower <= number <= upper:
            raise ValueError("Invalid display " + key)
    if value["scale_a"] <= 0 or type(value["sample_rate_hz"]) is not int or value["sample_rate_hz"] != 20000:
        raise ValueError("Expected positive scale and 20 kHz display sample rate")
    if value["sim_time_s"] < .025:
        raise ValueError("Display time must contain a complete 25 ms window")
    return dict(value)


def parse_window(data):
    required = {"run_id", "input_id", "samples_b64", "sha256"}
    if not isinstance(data, dict) or not required <= set(data) or set(data) - required - {"display"}:
        raise ValueError("Send waveform fields and optional display units/time only; no class labels")
    display_metadata(data)
    if type(data["input_id"]) is not int or not 1 <= data["input_id"] <= 100000:
        raise ValueError("Invalid input_id")
    encoded = data["samples_b64"]
    if not isinstance(encoded, str) or len(encoded) != 8000:
        raise ValueError("Expected 6000 bytes: 3 x 500 little-endian float32 samples")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise ValueError("Invalid base64 waveform")
    if len(raw) != 6000 or hashlib.sha256(raw).hexdigest() != data["sha256"]:
        raise ValueError("Waveform length or SHA256 mismatch")
    if not all(math.isfinite(v) and -1 <= v <= 1 for v in struct.unpack("<1500f", raw)):
        raise ValueError("Waveform must contain finite normalized samples in [-1,1]")
    return raw


class WindowStream:
    def __init__(self, idle_seconds=5):
        self.condition = threading.Condition()
        self.pending = None
        self.last_id = 0
        self.closed = None
        self.idle_seconds = idle_seconds

    def submit(self, data, timeout=12):
        raw = parse_window(data)
        item = dict(raw=raw, context=dict(input_id=data["input_id"], sha256=data["sha256"],
                                        bytes=len(raw), transport="ethernet-f32"),
                    done=threading.Event(), claimed=False, result=None, error=None)
        display = display_metadata(data)
        if display is not None:
            item["context"]["display"] = display
        with self.condition:
            if self.closed:
                raise RuntimeError(self.closed)
            if self.pending or data["input_id"] != self.last_id + 1:
                raise RuntimeError("One outstanding window; input_id must increase by one")
            self.last_id = data["input_id"]
            self.pending = item
            self.condition.notify_all()
        if not item["done"].wait(timeout):
            self.close("Input processing timeout; start a new stream")
            raise RuntimeError("Input processing timeout; start a new stream")
        if item["error"]:
            raise RuntimeError(item["error"])
        return item["result"]

    def take(self, stop_event):
        deadline = time.monotonic() + self.idle_seconds
        with self.condition:
            while True:
                if stop_event.is_set():
                    return None
                if self.closed:
                    raise RuntimeError(self.closed)
                if self.pending and not self.pending["claimed"]:
                    self.pending["claimed"] = True
                    return self.pending
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("No Ethernet waveform for 5 seconds; stream expired")
                self.condition.wait(min(remaining, .1))

    def complete(self, item, result):
        with self.condition:
            if self.pending is item and not self.closed:
                item["result"] = result
                self.pending = None
                item["done"].set()
                self.condition.notify_all()

    def close(self, reason="Input stream stopped"):
        with self.condition:
            self.closed = reason
            if self.pending:
                self.pending["error"] = reason
                self.pending["done"].set()
                self.pending = None
            self.condition.notify_all()
