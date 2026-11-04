"""Verify a transferred export without importing torch or numpy."""
import argparse,hashlib,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('directory',type=Path);a=p.parse_args()
manifest=json.loads((a.directory/'manifest.json').read_text(encoding='utf-8'))
files=manifest['files']
if not files: raise SystemExit('No files in manifest')
for name,expected in files.items():
    f=a.directory/name
    if f.resolve().parent!=a.directory.resolve(): raise SystemExit('Invalid manifest path')
    actual=hashlib.sha256(f.read_bytes()).hexdigest()
    if actual!=expected: raise SystemExit('Hash mismatch: '+name)
print('Verified',len(files),'artifact hashes')
