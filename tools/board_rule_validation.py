"""Native PS/NPU rule regression. Raw output stays in the board result store."""

import argparse
import json
from pathlib import Path
import time
import urllib.request
from board_acceptance import resources, dump


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--server-pid", type=int, required=True)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)

    def api(path, body=None, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-PowerScope-Token"] = token
        req = urllib.request.Request(
            "http://127.0.0.1:8765" + path,
            data=None if body is None else json.dumps(body).encode(),
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.load(r)

    start = api("/api/state")
    assert start["status"] == "idle"
    assert start["sensor_rules"]["enabled"] and start["statistics"] is False
    token = start["csrf"]
    session = start["session"]
    store = Path(start["storage"])
    before = resources(a.server_pid, store)
    dump(a.output / "resources-before.json", before)
    status = dict(
        status="running",
        session=session,
        server_pid=a.server_pid,
        rules=start["sensor_rules"],
        runs=[],
        http_errors=0,
    )
    dump(a.output / "status.json", status)
    identity = None
    try:
        for stage, dataset, repeat, period in [
            ("validation", "validation", 1, 0),
            ("heldout", "heldout", 1, 0),
            ("repeat_validation", "validation", 3, 25),
        ]:
            rid = api(
                "/api/start",
                dict(dataset=dataset, repeat=repeat, period_ms=period),
                token,
            )["run_id"]
            status.update(stage=stage, run_id=rid)
            dump(a.output / "status.json", status)
            begin = time.monotonic()
            last_sample = last_progress = 0
            with (a.output / (stage + "-resources.jsonl")).open("x") as log:
                while True:
                    try:
                        s = api("/api/state")
                    except Exception:
                        status["http_errors"] += 1
                        raise
                    now = time.monotonic()
                    assert s["session"] == session and s["run_id"] == rid
                    assert (
                        s["sensor_rules"] == status["rules"]
                        and s["axi"]["mode"] == "preview"
                    )
                    if now - last_sample >= 5 or s["status"] not in [
                        "starting",
                        "running",
                    ]:
                        resource = resources(a.server_pid, store)
                        assert resource["boot_id"] == before["boot_id"]
                        assert (
                            resource["service"]["start_ticks"]
                            == before["service"]["start_ticks"]
                        )
                        if s["worker_ready"]:
                            assert len(resource["workers"]) == 1
                            current = (
                                resource["workers"][0]["pid"],
                                resource["workers"][0]["start_ticks"],
                            )
                            if identity is None:
                                identity = current
                            assert identity == current
                        resource.update(
                            stage=stage,
                            processed=s["processed"],
                            npu_correct=s["npu_correct"],
                            correct=s["correct"],
                            elapsed_seconds=now - begin,
                        )
                        log.write(json.dumps(resource) + "\n")
                        log.flush()
                        last_sample = now
                    if now - last_progress >= 15 or s["status"] not in [
                        "starting",
                        "running",
                    ]:
                        status.update(
                            processed=s["processed"],
                            correct=s["correct"],
                            npu_correct=s["npu_correct"],
                            elapsed_seconds=now - begin,
                        )
                        dump(a.output / "status.json", status)
                        print(
                            stage,
                            s["processed"],
                            s["npu_correct"],
                            s["correct"],
                            s["status"],
                            flush=True,
                        )
                        last_progress = now
                    if s["status"] not in ["starting", "running"]:
                        assert s["status"] == "completed", s.get("error")
                        assert (
                            s["processed"] == s["total"] and s["mailbox"]["flags"] == 4
                        )
                        status["runs"].append(
                            dict(
                                stage=stage,
                                run_id=rid,
                                late_frames=s["late_frames"],
                                manifest=json.loads(
                                    (store / rid / "manifest.json").read_text()
                                ),
                            )
                        )
                        break
                    assert now - begin < 300, "Validation timeout"
                    time.sleep(0.3)
        status.update(status="completed", worker_identity=identity)
    except Exception as e:
        status.update(status="error", error=str(e))
        raise
    finally:
        dump(a.output / "status.json", status)
        dump(a.output / "resources-after.json", resources(a.server_pid, store))
    print("Saved native results; analyze raw rows before accepting", flush=True)


if __name__ == "__main__":
    main()
