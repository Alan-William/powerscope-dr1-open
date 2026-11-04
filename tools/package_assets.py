"""Create a verified transfer archive from manifest-listed artifacts only."""
import argparse,hashlib,json,tarfile
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('directory',type=Path);p.add_argument('archive',type=Path);a=p.parse_args()
manifest=json.loads((a.directory/'manifest.json').read_text())
for name,digest in manifest['files'].items():
    f=a.directory/name
    if f.resolve().parent!=a.directory.resolve():raise SystemExit('Unsafe manifest path')
    if hashlib.sha256(f.read_bytes()).hexdigest()!=digest:raise SystemExit('Hash mismatch: '+name)
a.archive.parent.mkdir(parents=True,exist_ok=True)
with tarfile.open(a.archive,'w:gz') as t:
    for name in ['manifest.json',*manifest['files']]:t.add(a.directory/name,arcname=name)
print(a.archive)
