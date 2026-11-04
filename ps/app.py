"""Board PS replay, HTTP display and durable local results. Python 3.8 stdlib.

The only inference engine is the persistent Alnpu worker. ABI preview is not
physical AXI or RS485. UIO access needs an explicit verified hardware contract.
"""

import argparse
from contextlib import nullcontext
import fcntl
import signal
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import select
import shutil
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from protocol.mailbox import packet, ERROR_NPU, ERROR_STORAGE, ERROR_AXI, ERROR_INPUT  # noqa: E402
from ps.axi import Publisher  # noqa: E402
from ps.sensor_rules import SensorRules  # noqa: E402
from ps.sample_transport import SampleTransport, SampleTransportError  # noqa: E402
from ps.result_store import ResultWriter  # noqa: E402
from ps.scenario import FaultScenario  # noqa: E402
from ps.network_input import WindowStream  # noqa: E402

LABELS = (
    ["Normal"]
    + [f"T{p}{n} OS" for p in "abc" for n in range(1, 5)]
    + [f"Sensor {p} {f}" for f in ("DF", "SF", "OF") for p in "ABC"]
)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def dump(path, value):
    temp = Path(str(path) + ".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(temp), str(path))


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def load_assets(path):
    path = Path(path).resolve()
    root = path.parent
    m = json.loads(path.read_text())
    if (
        m.get("schema") != 1
        or m["model"].get("id") not in {"kd-s1", "sfaug-r1"}
        or m["model"].get("statistics") is not False
    ):
        raise ValueError("Expected a supported student asset manifest with statistics disabled")
    for spec in list(m["model"].values()) + list(m["datasets"].values()):
        if not isinstance(spec, dict) or "path" not in spec:
            continue
        f = (root / spec["path"]).resolve()
        if root not in f.parents or sha(f) != spec["sha256"]:
            raise ValueError("Asset path/hash mismatch: " + spec["path"])
        spec["absolute_path"] = str(f)
    for d in m["datasets"].values():
        if (
            d["count"] * 6000 != Path(d["absolute_path"]).stat().st_size
            or len(d["labels"]) != d["count"]
            or len(d["dataset_indices"]) != d["count"]
        ):
            raise ValueError("Dataset length mismatch")
        if any(type(y) is not int or not 0 <= y < 22 for y in d["labels"]):
            raise ValueError("Dataset labels invalid")
    return m


class Worker:
    def __init__(self, binary, vendor_lib, model, evidence):
        self.log = Path(str(evidence) + ".stderr.log").open("ab")
        env = os.environ.copy()
        env["LD_LIBRARY_PATH"] = str(vendor_lib)
        self.proc = subprocess.Popen(
            [
                str(binary),
                model["trunk"]["absolute_path"],
                model["head"]["absolute_path"],
                str(evidence),
                model["id"],
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.log,
            bufsize=0,
            env=env,
        )
        self.buffer = b""
        try:
            ready = self.read(25)
            if (
                ready.get("type") != "ready"
                or ready.get("backend") != "Alnpu"
                or ready.get("statistics") is not False
                or ready.get("model") != model["id"]
                or ready.get("input_samples") != 1500
                or ready.get("output_classes") != 22
            ):
                raise RuntimeError("Worker identity mismatch")
        except Exception:
            self.close()
            raise

    def read(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            if b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                try:
                    record = json.loads(line)
                except ValueError:
                    raise RuntimeError("Invalid worker response")
                return record
            remaining = deadline - time.monotonic()
            if (
                remaining <= 0
                or not select.select([self.proc.stdout], [], [], remaining)[0]
            ):
                raise TimeoutError("NPU worker response timeout")
            block = os.read(self.proc.stdout.fileno(), 65536)
            if not block:
                raise RuntimeError("NPU worker exited; see worker stderr")
            self.buffer += block
            if len(self.buffer) > 65536:
                raise RuntimeError("Worker response too large")

    def infer(self, sequence, raw):
        if len(raw) != 6000:
            raise ValueError("Expected 1500 float32 samples")
        payload = b"PSF1" + struct.pack("<I", sequence) + raw
        while payload:
            n = os.write(self.proc.stdin.fileno(), payload)
            if not n:
                raise RuntimeError("Worker input closed")
            payload = payload[n:]
        r = self.read(5)
        if (
            r.get("type") != "result"
            or r.get("backend") != "Alnpu"
            or r.get("sequence") != sequence
            or len(r.get("logits", [])) != 22
            or not all(math.isfinite(x) for x in r["logits"])
        ):
            raise RuntimeError("Invalid NPU result")
        return r

    def close(self):
        if self.proc.poll() is None:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc.stdout.close()
        self.log.close()


class Replay:
    def __init__(self, args):
        self.args = args
        rules_path = getattr(args, "sensor_rules", None)
        self.sensor_rules = SensorRules(rules_path) if rules_path else None
        self.rule_policy = (
            self.sensor_rules.public() if self.sensor_rules else {"enabled": False}
        )
        self.max_frames = getattr(args, "max_frames", 10000)
        if type(self.max_frames) is not int or not 1 <= self.max_frames <= 100000:
            raise ValueError("max_frames must be 1..100000")
        self.assets = load_assets(args.assets)
        self.store = Path(args.store).resolve()
        self.store.mkdir(parents=True, exist_ok=True)
        self.store_lock = (self.store / ".app.lock").open("a")
        fcntl.flock(self.store_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        for old in self.store.glob("*/manifest.json"):
            value = json.loads(old.read_text())
            if value.get("status") == "running":
                value.update(
                    status="interrupted",
                    error="Previous PS process ended before finalization",
                )
                dump(old, value)
        self.guard = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.scenario = None
        self.network_input = None
        self.result_writer = None
        self.worker = None
        self.sequence = 0
        self.session = secrets.token_hex(8)
        self.csrf = secrets.token_hex(24)
        self.publisher = Publisher(
            json.loads(Path(args.axi_config).read_text()) if args.axi_config else None,
            args.mmio_lib,
        )
        waveform_config = getattr(args, "waveform_config", None)
        if waveform_config and not args.axi_config:
            raise ValueError("PL sample input requires the verified result mailbox too")
        if getattr(args, "recover_interrupted_waveform", False) and not waveform_config:
            raise ValueError("Startup waveform recovery requires verified physical UIO")
        self.sample_transport = SampleTransport(
            json.loads(Path(waveform_config).read_text()) if waveform_config else None,
            args.mmio_lib,
            recover_on_start=getattr(args, "recover_interrupted_waveform", False),
        )
        recovery = self.sample_transport.startup_recovery
        if recovery is not None:
            dump(self.store / ("startup-" + self.session + ".json"),
                 dict(session=self.session, source_revision=args.revision,
                      boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                      waveform_recovery=recovery))
        self.state = {
            "status": "idle",
            "run_id": None,
            "processed": 0,
            "correct": 0,
            "npu_correct": 0,
            "rule_matches": 0,
            "rule_changes": 0,
            "rule_improvements": 0,
            "rule_regressions": 0,
            "total": 0,
            "latest": None,
            "recent": [],
            "error": None,
            "mailbox": packet(0),
            "axi": {
                "mode": "uio" if args.axi_config else "preview",
                "physical": False,
                "ack_token": None,
            },
            "late_frames": 0,
            "sample_transport": {"mode": self.sample_transport.mode, "physical": False},
        }
        self.audit = {
            "result_schema": 2,
            "model_id": self.assets["model"]["id"],
            "trunk_sha256": self.assets["model"]["trunk"]["sha256"],
            "head_sha256": self.assets["model"]["head"]["sha256"],
            "worker_sha256": sha(args.worker),
            "assets_manifest_sha256": sha(args.assets),
            "session": self.session,
            "simulated": True,
            "statistics": False,
            "sensor_rules": self.rule_policy,
            "score_semantics": "NPU softmax at final published label, not hybrid confidence",
            "board_wall_clock_trusted": False,
            "source_revision": args.revision,
            "transport_mode": "uio" if args.axi_config else "preview",
            "input_transport": self.sample_transport.mode,
            "waveform_config_sha256": sha(waveform_config) if waveform_config else None,
            "waveform_startup_recovery": recovery,
        }

    def public(self):
        with self.guard:
            s = json.loads(json.dumps(self.state))
            s.update(
                model=self.assets["model"]["id"],
                backend="Alnpu",
                simulated=True,
                statistics=False,
                sensor_rules=self.rule_policy,
                labels=LABELS,
                csrf=self.csrf,
                worker_ready=bool(self.worker and self.worker.proc.poll() is None),
                datasets={
                    k: {"count": v["count"], "title": v["title"]}
                    for k, v in self.assets["datasets"].items()
                },
                storage=str(self.store),
                storage_io=self.result_writer.snapshot() if self.result_writer else None,
                session=self.session,
                max_frames=self.max_frames,
                scenario=self.scenario.public() if self.scenario else {"enabled": False},
                external_input={"supported": True, "enabled": bool(self.network_input),
                                "active": bool(self.network_input and self.state["status"] in ("starting", "running")),
                                "format": "3x500-f32le-normalized", "idle_timeout_s": 5,
                                "display_metadata_version": 1},
            )
            return s

    def ensure_worker(self):
        if self.worker is None:
            self.worker = Worker(
                self.args.worker,
                self.args.vendor_lib,
                self.assets["model"],
                self.store / ("worker-" + secrets.token_hex(8)),
            )

    def start(self, dataset, repeat=1, period_ms=25, monitor=False, external=False):
        if not isinstance(dataset, str) or dataset not in self.assets["datasets"]:
            raise ValueError("Unknown dataset")
        if (
            type(repeat) is not int
            or not 1 <= repeat <= 100
            or type(period_ms) not in (int, float)
            or not math.isfinite(period_ms)
            or not 0 <= period_ms <= 1000
        ):
            raise ValueError("Invalid replay controls")
        if type(monitor) is not bool:
            raise ValueError("monitor must be a boolean")
        if monitor and (self.sensor_rules or period_ms < 75 or repeat != 1):
            raise ValueError("Operating scenario requires pure NPU, period >= 75 ms and repeat=1")
        if external and (self.sensor_rules or monitor or repeat != 1 or period_ms != 0):
            raise ValueError("Ethernet input requires pure NPU and one outstanding window")
        total = self.max_frames if monitor or external else self.assets["datasets"][dataset]["count"] * repeat
        if total > self.max_frames:
            raise ValueError("Maximum %d frames per run" % self.max_frames)
        with self.guard:
            if self.thread and self.thread.is_alive():
                raise RuntimeError("Replay already active")
            if self.result_writer and not self.result_writer.done.is_set():
                raise RuntimeError("Previous result writer is still finishing")
            if shutil.disk_usage(self.store).free < 200 * 1024**2:
                raise OSError("Less than 200 MiB storage available")
            scenario = FaultScenario(self.assets["datasets"][dataset]["labels"], time.monotonic()) if monitor else None
            rid = secrets.token_hex(12)
            folder = self.store / rid
            folder.mkdir()
            self.stop_event.clear()
            self.result_writer = None
            self.scenario = scenario
            self.network_input = WindowStream() if external else None
            self.state.update(
                status="starting",
                run_id=rid,
                processed=0,
                correct=0,
                npu_correct=0,
                rule_matches=0,
                rule_changes=0,
                rule_improvements=0,
                rule_regressions=0,
                total=total,
                latest=None,
                recent=[],
                error=None,
                late_frames=0,
                period_ms=period_ms,
                dataset=None if external else dataset,
            )
            self.thread = threading.Thread(
                target=self.run, args=(folder, dataset, repeat, period_ms), daemon=True
            )
            self.thread.start()
            return rid

    def start_external(self):
        if self.sample_transport.mode != "pl-q15" or not self.args.axi_config:
            raise ValueError("Ethernet demo requires verified PL waveform input and AXI mailbox")
        return self.start(next(iter(self.assets["datasets"])), period_ms=0, external=True)

    def submit_external(self, data):
        with self.guard:
            stream = self.network_input
            if (not stream or data.get("run_id") != self.state["run_id"]
                    or self.state["status"] not in ("starting", "running") or self.stop_event.is_set()):
                raise RuntimeError("No matching active Ethernet input run")
        return stream.submit(data)

    def stop_external(self, run_id):
        with self.guard:
            if not self.network_input or run_id != self.state["run_id"]:
                raise RuntimeError("No matching Ethernet input run")
            self.stop()

    def inject(self, label):
        with self.guard:
            if not self.scenario or self.state["status"] != "running" or self.stop_event.is_set():
                raise RuntimeError("Start the operating scenario before injecting a state")
            return self.scenario.request(label, time.monotonic())

    def invalidate(self, error=0):
        p = packet(self.sequence, error=error)
        result = self.publisher.publish(p)
        with self.guard:
            self.state["mailbox"] = p
            self.state["axi"] = result
        return p

    def run(self, folder, dataset, repeat, period_ms):
        d = self.assets["datasets"][dataset]
        network = self.network_input
        begin = time.monotonic()
        failure = None
        error_code = ERROR_STORAGE
        terminal = "completed"
        info = dict(
            self.audit,
            dataset=None if network else dataset,
            dataset_sha256=None if network else d["sha256"],
            repeat=repeat,
            period_ms=period_ms,
            board_started_utc=utc(),
            status="running",
            policy="one outstanding inference; no catch-up burst; slow replay if processing exceeds period; stop at run limit",
            mode="ethernet-waveform" if network else "operating-scenario" if self.scenario else "dataset-validation",
        )
        writer = None
        processed = correct = npu_correct = rule_matches = rule_changes = 0
        rule_improvements = rule_regressions = 0
        try:
            dump(folder / "manifest.json", info)
            error_code = ERROR_AXI
            self.invalidate()
            error_code = ERROR_NPU
            self.ensure_worker()
            error_code = ERROR_STORAGE
            writer = ResultWriter(folder,
                [
                    "sequence",
                    "dataset_index",
                    "label",
                    "label_name",
                    "expected",
                    "correct",
                    "score",
                    "inference_us",
                    "worker_us",
                    "ps_application_us",
                    "axi_mode",
                    "ack_token",
                    "npu_label",
                    "npu_label_name",
                    "npu_score",
                    "npu_correct",
                    "diagnosis_source",
                    "rule_matched",
                    "rule_changed",
                    "rule_reason",
                    "rule_us",
                    "event_kind",
                    "input_request_id",
                    "input_label",
                    "input_selected_ms",
                    "result_published_ms",
                    "network_input_id",
                    "network_input_sha256",
                ]
            )
            self.result_writer = writer
            with self.guard:
                self.state["status"] = "running"
            with (nullcontext(None) if network else open(d["absolute_path"], "rb")) as source:
                for cycle in range(repeat):
                    if source:
                        source.seek(0)
                    for index in range(self.max_frames if self.scenario or network else d["count"]):
                        if self.stop_event.is_set():
                            terminal = "stopped"
                            break
                        error_code = ERROR_STORAGE
                        writer.check_capacity()
                        network_item = None
                        network_context = None
                        error_code = ERROR_INPUT
                        if network:
                            network_item = network.take(self.stop_event)
                            if network_item is None:
                                terminal = "stopped"
                                break
                            network_context = network_item["context"]
                        t0 = time.monotonic()
                        scenario_input = None
                        if self.scenario:
                            with self.guard:
                                index, scenario_input = self.scenario.select(t0)
                            source.seek(index * 6000)
                        error_code = ERROR_INPUT
                        raw = network_item["raw"] if network else source.read(6000)
                        if len(raw) != 6000:
                            raise ValueError("Truncated replay dataset")
                        next_sequence = (self.sequence + 1) & 0xFFFFFFFF
                        try:
                            raw, sample_link = self.sample_transport.transfer(next_sequence, raw)
                        except SampleTransportError:
                            error_code = ERROR_AXI
                            raise
                        rule_begin = time.monotonic()
                        wave = struct.unpack("<1500f", raw)
                        if network_context:
                            network_context["npu_input_sha256"] = hashlib.sha256(raw).hexdigest()
                        rule = (
                            self.sensor_rules.diagnose(wave)
                            if self.sensor_rules
                            else dict(
                                matched=False,
                                candidate_label=None,
                                reason="disabled",
                                phase=None,
                                kind=None,
                            )
                        )
                        rule_us = (
                            int((time.monotonic() - rule_begin) * 1e6)
                            if self.sensor_rules
                            else 0
                        )
                        self.sequence = next_sequence
                        error_code = ERROR_NPU
                        r = self.worker.infer(self.sequence, raw)
                        npu_top = max(range(22), key=lambda i: r["logits"][i])
                        exp = [math.exp(v - max(r["logits"])) for v in r["logits"]]
                        top = rule["candidate_label"] if rule["matched"] else npu_top
                        rule["changed"] = top != npu_top
                        diagnosis_source = "ps_rule" if rule["matched"] else "npu"
                        score = exp[top] / sum(exp)
                        npu_score = exp[npu_top] / sum(exp)
                        p = packet(
                            self.sequence, top, score, r["inference_us"], npu=True
                        )
                        error_code = ERROR_AXI
                        link = self.publisher.publish(p)
                        application_us = int((time.monotonic() - t0) * 1e6)
                        event = None
                        if self.scenario:
                            with self.guard:
                                published = time.monotonic()
                                event = self.scenario.observe(npu_top, self.sequence, published, scenario_input)
                                scenario_input["published_ms"] = self.scenario.elapsed(published)
                        expected = None if network else d["labels"][index]
                        hit = None if network else top == expected
                        npu_hit = None if network else npu_top == expected
                        row = dict(
                            r,
                            model_id=self.assets["model"]["id"],
                            run_id=folder.name,
                            simulated=True,
                            dataset=None if network else dataset,
                            dataset_index=None if network else d["dataset_indices"][index],
                            cycle=cycle,
                            label=top,
                            label_name=LABELS[top],
                            expected=expected,
                            correct=hit,
                            npu_label=npu_top,
                            npu_label_name=LABELS[npu_top],
                            npu_score=npu_score,
                            npu_correct=npu_hit,
                            diagnosis_source=diagnosis_source,
                            rule=rule,
                            rule_us=rule_us,
                            score=score,
                            score_semantics="npu_softmax_at_final_label",
                            score_calibrated=False,
                            elapsed_ms=round((time.monotonic() - begin) * 1000, 3),
                            ps_application_us=application_us,
                            mailbox=p,
                            axi=link,
                            sample_transport=sample_link,
                            scenario_input=scenario_input,
                            event=event,
                            network_input=network_context,
                        )
                        error_code = ERROR_STORAGE
                        writer.submit(row,
                            [
                                self.sequence,
                                row["dataset_index"],
                                top,
                                LABELS[top],
                                row["expected"],
                                int(hit) if hit is not None else "",
                                score,
                                r["inference_us"],
                                r["worker_us"],
                                application_us,
                                link["mode"],
                                link["ack_token"],
                                npu_top,
                                LABELS[npu_top],
                                npu_score,
                                int(npu_hit) if npu_hit is not None else "",
                                diagnosis_source,
                                int(rule["matched"]),
                                int(rule["changed"]),
                                rule["reason"],
                                rule_us,
                                event["kind"] if event else "",
                                scenario_input["request_id"] if scenario_input else "",
                                scenario_input["input_label"] if scenario_input else "",
                                scenario_input["selected_ms"] if scenario_input else "",
                                scenario_input["published_ms"] if scenario_input else "",
                                network_context["input_id"] if network_context else "",
                                network_context["sha256"] if network_context else "",
                            ]
                        )
                        processed += 1
                        correct += int(bool(hit))
                        npu_correct += int(bool(npu_hit))
                        rule_matches += int(rule["matched"])
                        rule_changes += int(rule["changed"])
                        rule_improvements += int(bool(hit and not npu_hit))
                        rule_regressions += int(bool(npu_hit and not hit))
                        with self.guard:
                            self.state.update(
                                processed=processed,
                                correct=correct,
                                npu_correct=npu_correct,
                                rule_matches=rule_matches,
                                rule_changes=rule_changes,
                                rule_improvements=rule_improvements,
                                rule_regressions=rule_regressions,
                                mailbox=p,
                                axi=link,
                                sample_transport=sample_link,
                                latest=dict(
                                    row,
                                    waveform=[
                                        list(wave[c * 500 : (c + 1) * 500])
                                        for c in range(3)
                                    ],
                                ),
                            )
                            self.state["recent"] = (
                                self.state["recent"]
                                + [
                                    {
                                        "sequence": self.sequence,
                                        "label_name": LABELS[top],
                                        "npu_label_name": LABELS[npu_top],
                                        "diagnosis_source": diagnosis_source,
                                        "rule_changed": rule["changed"],
                                        "expected_name": LABELS[expected] if expected is not None else None,
                                        "correct": hit,
                                        "inference_us": r["inference_us"],
                                        "ps_application_us": application_us,
                                    }
                                ]
                            )[-20:]
                            if period_ms and (time.monotonic() - t0) * 1000 > period_ms:
                                self.state["late_frames"] += 1
                        if network:
                            network.complete(network_item, dict(
                                run_id=folder.name, sequence=self.sequence,
                                **network_context, registers=p["registers"],
                                sample_transport=sample_link, axi=link, model=self.assets["model"]["id"]))
                        self.stop_event.wait(
                            max(0, period_ms / 1000 - (time.monotonic() - t0))
                        )
                    if self.stop_event.is_set():
                        terminal = "stopped"
                        break
        except Exception as e:
            failure = str(e)
            terminal = "error"
            if self.worker:
                self.worker.close()
                self.worker = None
        finally:
            if network:
                network.close(failure or "Input stream stopped")
            with self.guard:
                self.state["status"] = "finalizing"
            try:
                self.invalidate(error_code if failure else 0)
            except Exception as e:
                failure = failure or str(e)
                terminal = "error"
                with self.guard:
                    self.state["mailbox"] = packet(self.sequence, error=ERROR_AXI)
                    self.state["axi"] = {
                        "mode": "uio",
                        "physical": False,
                        "error": str(e),
                        "ack_token": None,
                    }
            # Invalidate before waiting for storage: never keep an old result
            # alive by republishing it while the SD card finishes writes.
            if writer:
                try:
                    writer.finish()
                except OSError as e:
                    if failure is None:
                        failure = str(e)
                        error_code = ERROR_STORAGE
                    terminal = "error"
                    if self.worker:
                        self.worker.close()
                        self.worker = None
                    try:
                        self.invalidate(error_code)
                    except Exception as invalidation_error:
                        with self.guard:
                            self.state["mailbox"] = packet(self.sequence, error=ERROR_AXI)
                            self.state["axi"] = dict(mode="uio", physical=False,
                                                     error=str(invalidation_error), ack_token=None)
            info.update(
                status=terminal,
                error=failure,
                processed=processed,
                correct=correct,
                accuracy=correct / processed if processed and not network else None,
                npu_correct=npu_correct,
                npu_accuracy=npu_correct / processed if processed and not network else None,
                rule_matches=rule_matches,
                rule_changes=rule_changes,
                rule_improvements=rule_improvements,
                rule_regressions=rule_regressions,
                elapsed_seconds=time.monotonic() - begin,
                board_finished_utc=utc(),
                storage_sync="bounded asynchronous writer; flush each row; fsync each 20 rows and on finish",
                storage_io=writer.snapshot() if writer else None,
                scenario=self.scenario.public() if self.scenario else {"enabled": False},
            )
            try:
                dump(folder / "manifest.json", info)
            except OSError as e:
                failure = failure or str(e)
                terminal = "error"
            with self.guard:
                self.state.update(status=terminal, error=failure)

    def stop(self):
        self.stop_event.set()
        if self.network_input:
            self.network_input.close()

    def close(self):
        self.stop()
        if self.thread:
            self.thread.join(timeout=20)
        if self.worker:
            self.worker.close()
            self.worker = None
        self.publisher.close()
        self.sample_transport.close()
        self.store_lock.close()


class Server(ThreadingHTTPServer):
    daemon_threads = True


def serve(app, host, port):
    web = Path(__file__).parent / "web"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(
            self, code, data, kind="application/json; charset=utf-8", filename=None
        ):
            if not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if filename:
                self.send_header(
                    "Content-Disposition", 'attachment; filename="' + filename + '"'
                )
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/api/state":
                return self.send(200, app.public())
            if path in ("/", "/app.js", "/style.css"):
                f = web / (
                    {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}[
                        path
                    ]
                )
                kind = (
                    "text/html; charset=utf-8"
                    if path == "/"
                    else (
                        "text/javascript; charset=utf-8"
                        if path.endswith(".js")
                        else "text/css; charset=utf-8"
                    )
                )
                return self.send(200, f.read_bytes(), kind)
            if path.startswith("/runs/"):
                parts = path.split("/")
                if (
                    len(parts) == 4
                    and len(parts[2]) == 24
                    and all(c in "0123456789abcdef" for c in parts[2])
                    and parts[3] in ("results.csv", "results.jsonl", "manifest.json")
                ):
                    f = app.store / parts[2] / parts[3]
                    if f.is_file():
                        kind = (
                            "text/csv; charset=utf-8"
                            if f.suffix == ".csv"
                            else (
                                "application/json"
                                if f.suffix == ".json"
                                else "application/x-ndjson"
                            )
                        )
                        return self.send(
                            200, f.read_bytes(), kind, parts[2] + "-" + parts[3]
                        )
            self.send(404, {"error": "Not found"})

        def do_POST(self):
            if self.headers.get("X-PowerScope-Token") != app.csrf:
                return self.send(403, {"error": "Invalid session token"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 10000:
                    raise ValueError("Invalid request length")
                data = json.loads(self.rfile.read(length))
                path = urlparse(self.path).path
                if not isinstance(data, dict):
                    raise ValueError("Expected JSON object")
                if path == "/api/stream/start":
                    return self.send(200, {"run_id": app.start_external()})
                if path == "/api/stream/window":
                    return self.send(200, app.submit_external(data))
                if path == "/api/stream/stop":
                    app.stop_external(data.get("run_id"))
                    return self.send(200, {"stopping": True})
                if path == "/api/start":
                    return self.send(
                        200,
                        {
                            "run_id": app.start(
                                data.get("dataset", "demo22"),
                                data.get("repeat", 1),
                                data.get("period_ms", 25),
                                data.get("monitor", False),
                            )
                        },
                    )
                if path == "/api/inject":
                    return self.send(200, {"request": app.inject(data.get("label"))})
                if path == "/api/stop":
                    app.stop()
                    return self.send(200, {"stopping": True})
                self.send(404, {"error": "Not found"})
            except (ValueError, KeyError) as e:
                self.send(400, {"error": str(e)})
            except (RuntimeError, OSError) as e:
                self.send(409, {"error": str(e)})

    return Server((host, port), Handler)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--assets", type=Path, required=True)
    p.add_argument("--worker", type=Path, required=True)
    p.add_argument("--vendor-lib", type=Path, required=True)
    p.add_argument("--store", type=Path, required=True)
    p.add_argument("--axi-config")
    p.add_argument("--waveform-config", help="Verified PL sample UIO; feed returned Q15 samples to rules and NPU")
    p.add_argument("--recover-interrupted-waveform", action="store_true",
                   help="On startup with exclusive UIO ownership, discard verified interrupted input; never retry a failed transfer")
    p.add_argument("--mmio-lib")
    p.add_argument("--revision", default="unversioned")
    p.add_argument(
        "--sensor-rules", type=Path, help="Opt in to a frozen PS rule configuration"
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument(
        "--max-frames",
        type=int,
        default=10000,
        help="Explicitly raise for bounded endurance runs; hard cap 100000",
    )
    a = p.parse_args()

    def terminate(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    app = Replay(a)
    server = None
    try:
        server = serve(app, a.host, a.port)
        print("PowerScope listening on http://%s:%s" % (a.host, a.port), flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.server_close()
        app.close()


if __name__ == "__main__":
    main()
