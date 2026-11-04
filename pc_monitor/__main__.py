"""Local-only HTTP bridge for the PC serial monitor. Run with python -m pc_monitor."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import secrets
import threading
from urllib.parse import parse_qs, urlsplit

from pc_monitor.monitor import Monitor, detected_ports
from pc_monitor.demo import Demo, ACTIVE
from pc_monitor.simulator import LiveSimulation

WEB = Path(__file__).resolve().parent / "web"


def make_server(monitor, port=18766, demo=None, simulation=None):
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond_headers(self, status, content_type, size, disposition=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            if disposition:
                self.send_header("Content-Disposition", disposition)
            self.end_headers()

        def json_response(self, value, status=200):
            raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.respond_headers(status, "application/json; charset=utf-8", len(raw))
            self.wfile.write(raw)

        def allowed_host(self):
            return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

        def do_GET(self):
            if not self.allowed_host():
                return self.json_response(dict(error="Invalid host"), 403)
            url = urlsplit(self.path)
            try:
                query = parse_qs(url.query)
                if url.path == "/api/state":
                    state = monitor.snapshot(max(0, int(query.get("after", [0])[0])), query.get("session", [None])[0])
                    state["token"] = token
                    state["demo"] = demo.snapshot() if demo else None
                    return self.json_response(state)
                if url.path == "/api/ports":
                    return self.json_response(dict(ports=detected_ports()))
                if url.path == "/api/simulation/state" and simulation:
                    state = simulation.snapshot()
                    if query.get("display") == ["matlab"]:
                        if state.get("latest"):
                            state["latest"] = dict(state["latest"])
                            for key in ("measured", "actual", "voltage"):
                                state["latest"][key] = [phase[::4] for phase in state["latest"][key]]
                        state["history"] = state["history"][::8]
                    serial = monitor.snapshot()
                    state.update(token=token, monitor={k: serial.get(k) for k in
                        ("link", "latest", "counts", "config", "last_good_age_ms")},
                        transport=demo.snapshot() if demo else None)
                    return self.json_response(state)
                if url.path in ("/api/export/csv", "/api/export/jsonl", "/api/demo/export", "/api/simulation/export"):
                    fmt = url.path.rsplit("/", 1)[1]
                    if url.path == "/api/simulation/export" and simulation:
                        fmt = "csv"
                        stream, name, remaining = simulation.export(query.get("session", [None])[0])
                    elif url.path == "/api/demo/export":
                        if not demo:
                            raise ValueError("Demo unavailable")
                        stream, name, remaining = demo.export(query.get("session", [None])[0])
                    else:
                        stream, name, remaining = monitor.export(fmt, query.get("session", [None])[0])
                    with stream:
                        self.respond_headers(200, "text/csv; charset=utf-8" if fmt == "csv" else "application/x-ndjson; charset=utf-8",
                                     remaining, f'attachment; filename="{name}"')
                        while remaining:
                            block = stream.read(min(65536, remaining))
                            if not block:
                                break
                            self.wfile.write(block)
                            remaining -= len(block)
                    return
                files = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/demo.js": "demo.js", "/style.css": "style.css", "/icon.svg": "icon.svg",
                         "/simulation": "simulation.html", "/simulation.html": "simulation.html",
                         "/simulation.js": "simulation.js", "/simulation.css": "simulation.css"}
                if url.path not in files:
                    return self.json_response(dict(error="Not found"), 404)
                path = WEB / files[url.path]
                raw = path.read_bytes()
                self.respond_headers(200, (mimetypes.guess_type(path)[0] or "application/octet-stream") + "; charset=utf-8", len(raw))
                self.wfile.write(raw)
            except (ValueError, OSError) as error:
                self.json_response(dict(error=str(error)), 400)

        def do_POST(self):
            origin = self.headers.get("Origin")
            allowed_origins = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
            if (not self.allowed_host() or (origin and origin not in allowed_origins)
                    or not secrets.compare_digest(self.headers.get("X-PowerScope-Token", ""), token)):
                return self.json_response(dict(error="请从本机监视器页面操作"), 403)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 <= size <= 2048:
                    raise ValueError("请求长度超限")
                body = json.loads(self.rfile.read(size) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("请求应为对象")
                if self.path == "/api/connect":
                    monitor.start(body.get("port"), body.get("period_ms", 100))
                elif self.path == "/api/disconnect":
                    if demo and demo.snapshot()["status"] in ACTIVE:
                        raise ValueError("请先停止演示，再断开 RS485")
                    monitor.stop()
                elif self.path == "/api/demo/start" and demo:
                    source = body.get("source", "library")
                    if source == "library" and simulation and simulation.snapshot()["status"] in ACTIVE:
                        raise ValueError("在线仿真正在运行，请在仿真页面接入板端，或先停止仿真")
                    demo.start(body.get("port"), body.get("automatic", False), source)
                elif self.path == "/api/demo/select" and demo:
                    demo.select(body.get("label"))
                elif self.path == "/api/demo/stop" and demo:
                    demo.stop()
                elif self.path == "/api/simulation/start" and simulation:
                    if demo and demo.snapshot()["status"] in ACTIVE:
                        raise ValueError("请先停止当前板端演示")
                    simulation.start(body.get("condition", 1), body.get("automatic", False))
                elif self.path == "/api/simulink/arm" and simulation and hasattr(simulation, "arm"):
                    if demo and demo.snapshot()["status"] in ACTIVE:
                        raise ValueError("请先停止板端传输")
                    result = simulation.arm(body.get("condition", 1), body.get("automatic", False),
                        body.get("duration_s"), body.get("warmup_s", 2.0), body.get("pace", True))
                    return self.json_response(result)
                elif self.path == "/api/simulink/error" and simulation and hasattr(simulation, "model_failed"):
                    simulation.model_failed(body.get("session"), body.get("error", "Simulink execution failed"))
                elif self.path == "/api/simulation/select" and simulation:
                    simulation.select(body.get("label"))
                elif self.path == "/api/simulation/stop" and simulation:
                    if demo and demo.source_kind == "live" and demo.snapshot()["status"] in ACTIVE:
                        demo.stop(wait=True)
                    simulation.stop()
                elif self.path == "/api/shutdown":
                    if demo:
                        demo.stop(wait=True)
                    if simulation:
                        simulation.stop(wait=True)
                    monitor.stop()
                    self.json_response(dict(ok=True))
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                else:
                    return self.json_response(dict(error="Not found"), 404)
                self.json_response(dict(ok=True))
            except (ValueError, OSError) as error:
                self.json_response(dict(error=str(error)), 400)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18766)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "artifacts/pc-rs485-monitor/captures")
    parser.add_argument("--demo-assets", type=Path, default=Path(__file__).resolve().parents[1] / "assets/pc-demo-manifest.json")
    parser.add_argument("--simulation-engine", choices=("native", "simulink"), default="native")
    parser.add_argument("--simulink-runtime", type=Path, default=Path("E:/fpga_workflow_runs/powerscope-simulink-runtime"))
    args = parser.parse_args()
    monitor = Monitor(args.output)
    if args.simulation_engine == "simulink":
        from pc_monitor.simulink import SimulinkSimulation
        simulation = SimulinkSimulation(runtime=args.simulink_runtime)
    else:
        simulation = LiveSimulation()
    demo = Demo(monitor, args.demo_assets, args.output.parent / "demos", simulator=simulation)
    server = make_server(monitor, args.port, demo, simulation)
    print(f"PowerScope RS485 monitor: http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        demo.stop(wait=True)
        simulation.stop(wait=True)
        monitor.stop()
        server.server_close()


if __name__ == "__main__":
    main()
