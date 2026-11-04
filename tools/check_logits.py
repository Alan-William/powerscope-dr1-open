"""Numerical check only: backend identity must be established separately."""
import argparse,hashlib,json
from pathlib import Path
import numpy as np

def compare(actual,expected,max_abs_error,min_top1_agreement):
    if actual.shape!=expected.shape or expected.ndim!=2 or expected.shape[1]!=22 or len(expected)==0:
        raise ValueError('Expected non-empty [frames,22] logits with identical shape')
    if not np.isfinite(actual).all() or not np.isfinite(expected).all():raise ValueError('Non-finite logits')
    if max_abs_error<0 or not np.isfinite(max_abs_error) or not 0<=min_top1_agreement<=1:raise ValueError('Invalid tolerance')
    error=float(np.max(abs(actual-expected)))
    agreement=float(np.mean(actual.argmax(1)==expected.argmax(1)))
    return {'frames':len(expected),'max_abs_error':error,'top1_agreement':agreement,
        'numeric_match':error<=max_abs_error and agreement>=min_top1_agreement,
        'limits':{'max_abs_error':max_abs_error,'min_top1_agreement':min_top1_agreement}}

def main():
    p=argparse.ArgumentParser();p.add_argument('actual',type=Path);p.add_argument('--reference',type=Path,required=True)
    p.add_argument('--max-abs-error',type=float,required=True);p.add_argument('--min-top1-agreement',type=float,default=1.0)
    p.add_argument('--source-label',default='unverified-backend');p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();expected=np.load(a.reference,allow_pickle=False)
    raw=a.actual.read_bytes()
    if len(raw)!=expected.size*4:raise SystemExit('Wrong raw output byte count')
    actual=np.frombuffer(raw,dtype='<f4').reshape(expected.shape)
    try:r=compare(actual,expected,a.max_abs_error,a.min_top1_agreement)
    except ValueError as e:raise SystemExit(str(e))
    r.update(scope='numeric check only; this does not attest NPU execution',source_label=a.source_label,
        actual_sha256=hashlib.sha256(raw).hexdigest(),reference_sha256=hashlib.sha256(a.reference.read_bytes()).hexdigest())
    a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(r,indent=2)+'\n')
    print(json.dumps(r,indent=2))
    if not r['numeric_match']:raise SystemExit(1)

if __name__=='__main__':main()
