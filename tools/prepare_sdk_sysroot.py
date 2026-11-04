"""Make absolute symlinks relocatable in a PRIVATE extracted board sysroot.

Does not modify the source rootfs archive, host /lib, or the board.
"""
import argparse
import os
from pathlib import Path

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sysroot", type=Path)
    ap.add_argument("crt_dir", type=Path)
    args = ap.parse_args()
    root = args.sysroot.resolve()
    if root == Path("/") or not (root / "usr/include/c++/9").is_dir():
        raise SystemExit("Expected a private extracted SDK sysroot with GCC 9 headers")
    changes = 0
    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            link = Path(parent) / name
            if not link.is_symlink():
                continue
            target = os.readlink(link)
            if not target.startswith("/"):
                continue
            relocated = root / target.lstrip("/")
            relative = os.path.relpath(relocated, link.parent)
            link.unlink()
            link.symlink_to(relative)
            changes += 1
    args.crt_dir.mkdir(parents=True, exist_ok=True)
    for name in ("Scrt1.o", "crti.o", "crtn.o", "crtbeginS.o", "crtendS.o"):
        directory = "usr/lib/gcc/aarch64-linux-gnu/9" if name.startswith("crtbegin") or name.startswith("crtend") else "usr/lib/aarch64-linux-gnu"
        target = root / directory / name
        if not target.is_file():
            raise SystemExit(f"Missing startup object: {target}")
        link = args.crt_dir / name
        if not link.is_symlink():
            link.symlink_to(target)
    print(f"Relocated {changes} absolute SDK symlinks; startup objects ready")

if __name__ == "__main__":
    main()
