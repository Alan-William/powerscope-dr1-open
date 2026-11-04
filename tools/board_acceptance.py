"""Board-local HTTP acceptance: held-out test followed by a single >30 min run.

Standard-library only; raw rows/resources stay outside Git. No MMIO or boot
changes. The monitor survives a workstation SSH disconnect.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import urllib.request


def dump(path, value):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(value, indent=2) + "\n")
    os.replace(str(tmp), str(path))


def process(pid):
    root = Path("/proc") / str(pid)
    stat = (root / "stat").read_text().rsplit(")", 1)[1].split()
    status = {
        line.split(":", 1)[0]: line.split(":", 1)[1].strip()
        for line in (root / "status").read_text().splitlines()
        if ":" in line
    }
    return dict(
        pid=pid,
        start_ticks=int(stat[19]),
        cpu_ticks=int(stat[11]) + int(stat[12]),
        rss_kib=int(status.get("VmRSS", "0 kB").split()[0]),
        threads=int(status["Threads"]),
        fd_count=len(list((root / "fd").iterdir())),
    )


def resources(pid, store):
    # The vendor kernel omits /proc/PID/task/PID/children. Resolve PPID from
    # proc stat instead of relying on CONFIG_CHECKPOINT_RESTORE.
    children = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[1]) == pid:
                children.append(entry.name)
        except (OSError, ValueError, IndexError):
            continue
    workers = []
    for child in children:
        cmd = (Path("/proc") / child / "cmdline").read_bytes()
        if cmd.split(b"\x00")[0].endswith(b"/ps_npu_worker"):
            workers.append(process(int(child)))
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        name, value = line.split(":", 1)
        if name in ["MemAvailable", "MemFree", "CmaTotal", "CmaFree", "SwapFree"]:
            memory[name + "_kib"] = int(value.strip().split()[0])
    temperatures = {}
    for zone in Path("/sys/class/thermal").glob("thermal_zone*"):
        try:
            temperatures[(zone / "type").read_text().strip()] = (
                int((zone / "temp").read_text()) / 1000
            )
        except (OSError, ValueError):
            pass
    return dict(
        monotonic=time.monotonic(),
        uptime=float(Path("/proc/uptime").read_text().split()[0]),
        boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        service=process(pid),
        workers=workers,
        memory=memory,
        free_bytes=shutil.disk_usage(store).free,
        temperatures_c=temperatures,
        loadavg=Path("/proc/loadavg").read_text().split()[:3],
        clock_ticks_per_second=os.sysconf("SC_CLK_TCK"),
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--server-pid", type=int, required=True)
    p.add_argument("--protocol", type=Path, required=True)
    p.add_argument("--base", default="http://127.0.0.1:8765")
    p.add_argument("--required-rules-sha256")
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)
    protocol = json.loads(a.protocol.read_text())
    assert protocol["endurance"]["total_frames"] == 72600
    assert protocol["endurance"]["minimum_first_to_last_result_seconds"] == 1800
    assert protocol["test"]["expected_frames"] == 4400
    shutil.copyfile(a.protocol, a.output / "protocol.json")

    def api(path, body=None, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-PowerScope-Token"] = token
        req = urllib.request.Request(
            a.base + path,
            None if body is None else json.dumps(body).encode(),
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.load(r)

    state = api("/api/state")
    if a.required_rules_sha256:
        assert state["sensor_rules"]["enabled"]
        assert state["sensor_rules"]["sha256"] == a.required_rules_sha256
        assert protocol["sensor_rules_sha256"] == a.required_rules_sha256
    assert state["status"] not in ["starting", "running"]
    assert (
        state["model"] == "kd-s1"
        and state["statistics"] is False
        and state["max_frames"] >= 72600
    )
    store = Path(state["storage"])
    token = state["csrf"]
    session = state["session"]
    before = resources(a.server_pid, store)
    dump(a.output / "resources-before.json", before)
    for name, args in [
        ("kernel-before", ["dmesg", "--color=never"]),
        ("interrupts-before", ["cat", "/proc/interrupts"]),
    ]:
        result = subprocess.run(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        (a.output / (name + ".txt")).write_text(result.stdout)
    status = dict(
        status="running",
        stage="heldout",
        protocol_sha256=hashlib.sha256(a.protocol.read_bytes()).hexdigest(),
        server_pid=a.server_pid,
        session=session,
        started_monotonic=time.monotonic(),
        http_errors=0,
        runs=[],
        sensor_rules=state.get("sensor_rules", {"enabled": False}),
    )
    dump(a.output / "status.json", status)
    worker_identity = None
    try:
        for kind, body, timeout in [
            ("heldout", dict(dataset="heldout", repeat=1, period_ms=0), 180),
            ("endurance", dict(dataset="validation", repeat=66, period_ms=25), 2700),
        ]:
            rid = api("/api/start", body, token)["run_id"]
            begin = time.monotonic()
            last_resource = last_progress = 0
            status.update(
                stage=kind, run_id=rid, processed=0, stage_started_monotonic=begin
            )
            dump(a.output / "status.json", status)
            print(kind, rid, "started", flush=True)
            with (a.output / (kind + "-resources.jsonl")).open("x") as log:
                while True:
                    poll_start = time.monotonic()
                    try:
                        s = api("/api/state")
                    except Exception:
                        status["http_errors"] += 1
                        raise
                    now = time.monotonic()
                    assert (
                        s["session"] == session and s["run_id"] == rid
                    ), "Service/run changed"
                    assert (
                        s.get("sensor_rules", {"enabled": False})
                        == status["sensor_rules"]
                    ), "Rule policy changed"
                    assert s["axi"]["mode"] == "preview" and not s["axi"]["physical"]
                    if now - last_resource >= 5 or s["status"] not in [
                        "starting",
                        "running",
                    ]:
                        sample = resources(a.server_pid, store)
                        sample.update(
                            stage=kind,
                            run_id=rid,
                            elapsed_seconds=now - begin,
                            processed=s["processed"],
                            correct=s["correct"],
                            status=s["status"],
                            late_frames=s["late_frames"],
                            http_ms=(now - poll_start) * 1000,
                        )
                        assert sample["boot_id"] == before["boot_id"], "Board rebooted"
                        assert (
                            sample["service"]["start_ticks"]
                            == before["service"]["start_ticks"]
                        ), "Service restarted"
                        if s["worker_ready"]:
                            assert (
                                len(sample["workers"]) == 1
                            ), "Expected exactly one NPU worker"
                            identity = (
                                sample["workers"][0]["pid"],
                                sample["workers"][0]["start_ticks"],
                            )
                            if worker_identity is None:
                                worker_identity = identity
                            assert identity == worker_identity, "Worker restarted"
                        assert (
                            sample["free_bytes"] >= 200 * 1024**2
                        ), "Storage reserve exhausted"
                        log.write(json.dumps(sample) + "\n")
                        log.flush()
                        last_resource = now
                    if now - last_progress >= 30 or s["status"] not in [
                        "starting",
                        "running",
                    ]:
                        status.update(
                            processed=s["processed"],
                            correct=s["correct"],
                            elapsed_seconds=now - begin,
                            worker_identity=worker_identity,
                            late_frames=s["late_frames"],
                            latest_state=s["status"],
                        )
                        dump(a.output / "status.json", status)
                        print(
                            kind,
                            s["processed"],
                            round(now - begin, 1),
                            s["status"],
                            flush=True,
                        )
                        last_progress = now
                    if s["status"] not in ["starting", "running"]:
                        assert s["status"] == "completed", s.get("error") or s["status"]
                        expected = 4400 if kind == "heldout" else 72600
                        assert s["processed"] == expected and s["total"] == expected
                        assert s["mailbox"]["flags"] == 4 and s["mailbox"]["error"] == 0
                        manifest = json.loads(
                            (store / rid / "manifest.json").read_text()
                        )
                        shutil.copyfile(
                            store / rid / "manifest.json",
                            a.output / (kind + "-manifest.json"),
                        )
                        status["runs"].append(
                            dict(
                                stage=kind,
                                run_id=rid,
                                processed=s["processed"],
                                correct=s["correct"],
                                elapsed_seconds=now - begin,
                                late_frames=s["late_frames"],
                                manifest=manifest,
                            )
                        )
                        break
                    assert now - begin < timeout, "Run timeout"
                    time.sleep(max(0, 0.3 - (time.monotonic() - poll_start)))
        status.update(status="completed", finished_monotonic=time.monotonic())
    except Exception as e:
        status.update(status="error", error=str(e), finished_monotonic=time.monotonic())
        try:
            api("/api/stop", {}, token)
        except Exception:
            pass
        raise
    finally:
        dump(a.output / "status.json", status)
        try:
            dump(a.output / "resources-after.json", resources(a.server_pid, store))
        except OSError:
            pass
        for name, args in [
            ("kernel-after", ["dmesg", "--color=never"]),
            ("interrupts-after", ["cat", "/proc/interrupts"]),
        ]:
            r = subprocess.run(
                args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
            (a.output / (name + ".txt")).write_text(r.stdout)
    print(
        "Completed acceptance workload; analyze stored rows before declaring pass",
        flush=True,
    )


if __name__ == "__main__":
    main()
