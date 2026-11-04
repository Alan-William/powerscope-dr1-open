"""Package the standalone Qt display locally, without deploying or changing boot files."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[1]
FILES = ["board_display/__init__.py", "board_display/__main__.py", "board_display/state.py",
         "ps/network_input.py", "protocol/labels.py", "board/deploy/run_display.sh",
         "docs/HDMI_TOUCH_DEMO.md"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/board-display-20261104/output")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    files = {name: (ROOT/name).read_bytes() for name in FILES}
    manifest = dict(schema=1, component="PowerScope HDMI Qt display", hardware_deployed=False,
                    model="sfaug-r1", ps_api="http://127.0.0.1:8765/api/state",
                    files={name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()})
    raw_manifest = (json.dumps(manifest, ensure_ascii=False, indent=2)+"\n").encode("utf-8")
    files["display-manifest.json"] = raw_manifest
    path = args.output/"powerscope-display-20261104.tar.gz"
    # Exclusive creation preserves earlier bundles instead of silently replacing them.
    with path.open("xb") as dest, tarfile.open(fileobj=dest, mode="w:gz") as archive:
        for name, raw in files.items():
            info = tarfile.TarInfo("powerscope-display-20261104/"+name)
            info.size = len(raw)
            info.mode = 0o755 if name.endswith('.sh') else 0o644
            archive.addfile(info, io.BytesIO(raw))
    with tarfile.open(path) as check:
        for name, raw in files.items():
            if check.extractfile("powerscope-display-20261104/"+name).read() != raw:
                raise RuntimeError("Bundle verification failed: " + name)
    (args.output/"display-manifest.json").write_bytes(raw_manifest)
    print(json.dumps(dict(path=str(path), bytes=path.stat().st_size,
                          sha256=hashlib.sha256(path.read_bytes()).hexdigest(), files=len(files))))


if __name__ == "__main__":
    main()
