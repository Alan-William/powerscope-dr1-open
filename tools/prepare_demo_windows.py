"""Extract existing frozen validation windows for the PC signal sender; no simulation."""
import argparse
import hashlib
import json
from pathlib import Path
import tarfile


def prepare(archive, destination):
    destination = Path(destination)
    with tarfile.open(archive) as source:
        manifest = json.load(source.extractfile("assets/manifest.json"))
        spec = manifest["datasets"]["validation"]
        raw = source.extractfile("assets/" + spec["path"]).read()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != spec["sha256"] or len(raw) != spec["count"] * 6000:
        raise ValueError("Frozen dataset hash/length mismatch")
    destination.mkdir(parents=True, exist_ok=True)
    output = dict(spec, schema=1, source="existing-v4-validation", title="已验证电流窗口 · 22 类 / 每类 50 窗",
                  model="sfaug-r1", sample_rate_hz=20000, channels=3, samples_per_channel=500,
                  format="float32-le-phase-major-normalized", simulated=True,
                  archive_sha256=hashlib.sha256(Path(archive).read_bytes()).hexdigest())
    for path, content in [(destination / spec["path"], raw),
                          (destination / "manifest.json", (json.dumps(output, ensure_ascii=False, indent=2) + "\n").encode())]:
        if path.exists() and path.read_bytes() != content:
            raise ValueError("Refusing to overwrite different demo assets: " + str(path))
        path.write_bytes(content)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output", type=Path, default=Path("artifacts/pc-demo-input"))
    args = parser.parse_args()
    value = prepare(args.archive, args.output)
    print(json.dumps(dict(output=str(args.output), count=value["count"], sha256=value["sha256"])))
