"""Append verified, explicitly named outputs to an existing package manifest."""
import argparse,datetime,hashlib,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('directory',type=Path);p.add_argument('--add',nargs='+',required=True);a=p.parse_args()
def sha(f):return hashlib.sha256(f.read_bytes()).hexdigest()
manifest=a.directory/'manifest.json';m=json.loads(manifest.read_text())
for name,digest in m['files'].items():
    if (a.directory/name).resolve().parent!=a.directory.resolve():raise SystemExit('Invalid path')
    if sha(a.directory/name)!=digest:raise SystemExit('Existing artifact changed: '+name)
for name in a.add:
    f=a.directory/name
    if f.resolve().parent!=a.directory.resolve() or name=='manifest.json':raise SystemExit('Invalid addition')
    m['files'][name]=sha(f)
if 'quantization.json' in m['files']:
    report=json.loads((a.directory/'quantization.json').read_text())
    if report['sha256'] not in m['files'].values():raise SystemExit('Evaluated model missing from package')
    if report['evaluation_count']!=1100:raise SystemExit('Expected full validation evaluation')
m['sealed_utc']=datetime.datetime.now(datetime.timezone.utc).isoformat()
manifest.write_text(json.dumps(m,indent=2)+'\n')
print('Sealed',len(m['files']),'files in',a.directory)
