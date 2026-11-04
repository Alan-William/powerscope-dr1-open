"""Run a prepared native candidate alone, then restore the established PS service.

Execute on the board after the rules endurance evidence has been preserved.
Never changes deployed weights, boot files, PL configuration, or devices.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import urllib.request


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def dump(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def api(path="/api/state", body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-PowerScope-Token"] = token
    req = urllib.request.Request(
        "http://127.0.0.1:8765" + path,
        data=None if body is None else json.dumps(body).encode(),
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.load(response)


def exited(proc):
    return (
        not proc.exists()
        or (proc / "stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--monitor", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--vendor-lib", type=Path, required=True)
    args = parser.parse_args()
    root, work = args.release.resolve(), args.work.resolve()
    prepared = json.loads((work / "prepared.json").read_text())
    assert not (work / "probe-status.json").exists(), "Use a fresh run directory"
    for name, digest in prepared["files_sha256"].items():
        path = (work / name).resolve()
        assert path.parent == work and sha(path) == digest, name
    assert (work / "native-input.f32").stat().st_size == 5500 * 9216 * 4
    assert sha(prepared["runner"]) == prepared["runner_sha256"]
    status = json.loads((args.monitor / "status.json").read_text())
    assert status["status"] == "completed" and status["http_errors"] == 0
    state = api()
    assert state["status"] == "completed" and state["session"] == status["session"]
    assert state["run_id"] == status["runs"][-1]["run_id"]
    assert state["max_frames"] == 100000 and state["sensor_rules"]["enabled"]
    assert state["sensor_rules"]["sha256"] == sha(root / "configs/sensor_rules_v1.json")
    pid = int((root / "server.pid").read_text())
    assert pid == status["server_pid"]
    proc = Path("/proc") / str(pid)
    command = (proc / "cmdline").read_bytes().decode().split("\0")[:-1]
    assert str(root / "ps/app.py") in command
    for name, digest in json.loads((root / "git-release.json").read_text())[
        "source_sha256"
    ].items():
        assert sha(root / name) == digest, name
    worker = Path("/proc") / str(status["worker_identity"][0])
    assert (
        int((worker / "stat").read_text().rsplit(")", 1)[1].split()[19])
        == status["worker_identity"][1]
    )
    dump(
        work / "previous-service.json",
        dict(
            pid=pid,
            command=command,
            session=state["session"],
            revision=(root / "source-revision.txt").read_text().strip(),
        ),
    )
    result = dict(status="starting", candidate=prepared, ps_rules_in_candidate=False)
    dump(work / "probe-status.json", result)
    os.kill(pid, signal.SIGTERM)
    try:
        for _ in range(100):
            if exited(proc) and exited(worker):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Previous service/worker did not exit")
        for name, cmd in [
            ("interrupts-before.txt", ["cat", "/proc/interrupts"]),
            ("kernel-before.txt", ["dmesg"]),
        ]:
            (work / name).write_bytes(subprocess.check_output(cmd))
        env = os.environ.copy()
        env["LD_LIBRARY_PATH"] = str(args.vendor_lib)
        start = time.monotonic()
        with (work / "native.log").open("xb") as log:
            run = subprocess.run(
                [
                    prepared["runner"],
                    str(work / "trunk.onnx"),
                    str(work / "head.onnx"),
                    str(work / "native-input.f32"),
                    str(work / "native"),
                    "Alnpu",
                ],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=900,
            )
        result.update(
            status="completed" if run.returncode == 0 else "failed",
            returncode=run.returncode,
            wall_seconds=time.monotonic() - start,
            boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        )
        for name, cmd in [
            ("interrupts-after.txt", ["cat", "/proc/interrupts"]),
            ("kernel-after.txt", ["dmesg"]),
        ]:
            (work / name).write_bytes(subprocess.check_output(cmd))
    except Exception as error:
        result.update(status="failed", error=str(error))
        raise
    finally:
        dump(work / "probe-status.json", result)
        # A still-running old process must not be duplicated.
        if not exited(proc) or not exited(worker):
            raise RuntimeError("Old service still alive; no duplicate service started")
        with (work / "restored-server.log").open("xb") as log:
            child = subprocess.Popen(
                [
                    "sh",
                    "board/deploy/run_ps_replay.sh",
                    "--sensor-rules",
                    str(root / "configs/sensor_rules_v1.json"),
                ],
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        (root / "server.pid").write_text(str(child.pid) + "\n")
        for _ in range(100):
            assert child.poll() is None
            try:
                state = api()
                break
            except OSError:
                time.sleep(0.1)
        assert state["status"] == "idle" and state["max_frames"] == 10000
        assert (
            state["sensor_rules"]["enabled"] and state["session"] != status["session"]
        )
        rid = api(
            "/api/start", dict(dataset="demo22", repeat=1, period_ms=25), state["csrf"]
        )["run_id"]
        for _ in range(300):
            state = api()
            if state["status"] not in ["starting", "running"]:
                break
            time.sleep(0.2)
        assert state["run_id"] == rid and state["status"] == "completed"
        assert state["processed"] == state["correct"] == 22 and state["error"] is None
        assert state["axi"]["mode"] == "preview" and not state["axi"]["physical"]
        result["restoration"] = dict(
            server_pid=child.pid,
            max_frames=10000,
            model_id=state["model"],
            rules=state["sensor_rules"],
            run_id=rid,
            processed=22,
            correct=22,
            session=state["session"],
            axi_mode="preview",
        )
        dump(work / "probe-status.json", result)
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
