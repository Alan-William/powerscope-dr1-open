"""Build ARM64 probes and audit the actual SDK under QEMU (Linux only).

Requires existing portable cross tools and an extracted board sysroot.
Never loads or executes a network on a hardware device. No CPU fallback.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import subprocess

def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--workspace',type=Path,required=True,help='Existing Ubuntu project artifact root')
    p.add_argument('--output',type=Path,required=True,help='Ignored directory for logs, ELF files and report')
    p.add_argument('--model',type=Path,required=True)
    a=p.parse_args();b=a.workspace.resolve();out=a.output.resolve();out.mkdir(parents=True,exist_ok=True)
    repo=Path(__file__).resolve().parents[1]
    cross=b/'.local/cross-tools/root'; sysroot=b/'.local/vendor/board-sysroot'; crt=b/'.local/compat-crt'
    sdk=b/'.local/vendor/app/face_detection/libs/armnn_lib'; lib=sdk/'lib'
    env=dict(os.environ,POWERSCOPE_CROSS_ROOT=str(cross),POWERSCOPE_BOARD_SYSROOT=str(sysroot),
        POWERSCOPE_CRT_DIR=str(crt),LD_LIBRARY_PATH=str(cross/'usr/lib/x86_64-linux-gnu'))
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    commands={}
    def run(name,args,required=False,timeout=120):
        r=subprocess.run([str(x) for x in args],capture_output=True,text=True,env=env,timeout=timeout)
        (out/(name+'.txt')).write_text(r.stdout+r.stderr)
        commands[name]={'argv':[str(x) for x in args],'returncode':r.returncode,'stdout':r.stdout,'stderr':r.stderr}
        if required and r.returncode:raise RuntimeError(f'{name} failed; inspect {out}/{name}.txt')
        return r
    run('prepare_sysroot',['python3',repo/'tools/prepare_sdk_sysroot.py',sysroot,crt],True)
    build=out/'aarch64'
    run('cmake_configure',['cmake','-S',repo/'board','-B',build,
        '-DCMAKE_TOOLCHAIN_FILE='+str(repo/'board/toolchains/aarch64-sdk.cmake'),
        '-DANLOGIC_ARMNN_ROOT='+str(sdk),'-DCMAKE_BUILD_TYPE=Release'],True)
    run('cmake_build',['cmake','--build',build,'-j2'],True)
    run('compiler_version',[cross/'usr/bin/aarch64-linux-gnu-g++','--version'],True)
    run('qemu_version',[cross/'usr/bin/qemu-aarch64','--version'],True)
    run('rootfs_version',['cat',sysroot/'usr/lib/os-release'],True)
    qemu=[cross/'usr/bin/qemu-aarch64','-cpu','max','-L',sysroot,'-E',
        f'LD_LIBRARY_PATH={lib}:{sysroot}/lib/aarch64-linux-gnu:{sysroot}/usr/lib/aarch64-linux-gnu']
    run('tensor_io_test',qemu+[build/'tensor_io_test'],True)
    inputs=b/'.local/vendor/app/face_detection/inputs'
    for name in ('yolo_face_uint8_15.onnx','fire_detect_sim_quant_uint8.onnx'):
        run('parse_'+Path(name).stem,qemu+[build/'parser_probe',inputs/name,'parse'],True)
    run('parse_candidate',qemu+[build/'parser_probe',a.model,'parse'],True)
    optimize=run('optimize_alnpu',qemu+[build/'parser_probe',a.model,'optimize-alnpu',out/'alnpu.dot'])
    # Keep device evidence concise; full strace remains only in the ignored log directory.
    trace=run('device_trace',qemu[:1]+['-strace']+qemu[1:]+[build/'parser_probe',a.model,'optimize-alnpu'])
    device_lines=[line for line in (trace.stdout+trace.stderr).splitlines() if '/dev/' in line]
    del commands['device_trace']
    binaries={}
    for name in ('parser_probe','npu_probe','tensor_io_test'):
        elf=build/name
        result=run(name+'_elf',['readelf','-h','-V','-d',elf],True)
        glibc=sorted(set(re.findall(r'GLIBC_(\d+\.\d+)',result.stdout)),key=lambda x:tuple(map(int,x.split('.'))))
        if 'AArch64' not in result.stdout or any(tuple(map(int,v.split('.')))>(2,31) for v in glibc):
            raise RuntimeError('Wrong architecture or glibc newer than supplied board rootfs')
        binaries[name]={'sha256':sha(elf),'bytes':elf.stat().st_size,'required_glibc':glibc}
    absent='None of the preferred backends [Alnpu ] are supported' in optimize.stderr
    report={'schema':1,'created_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'scope':'Real vendor parser on emulated AArch64 CPU; no hardware inference',
        'model_sha256':sha(a.model),'binaries':binaries,
        'source_hashes':{name:sha(repo/name) for name in ('board/CMakeLists.txt','board/parser_probe.cpp',
            'board/npu_probe.cpp','board/tensor_io.hpp','board/tensor_io_test.cpp',
            'board/toolchains/aarch64-sdk.cmake','tools/prepare_sdk_sysroot.py',
            'tools/validate_vendor_sdk.py','ml/normalize_vendor_onnx.py')},
        'sdk_hashes':{name:sha(lib/name) for name in ('libarmnn.so.32.1','libarmnnOnnxParser.so.24.6','libprotobuf.so.23.0.0')},
        'checks':{'compile_link':True,'vendor_positive_controls':True,'candidate_parse':True,'tensor_io':True,
            'alnpu_optimize':optimize.returncode==0,'alnpu_backend_unavailable':absent,'hardware_inference':False},
        'device_probe_evidence':device_lines,'commands':commands}
    (out/'sdk_validation.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='commands'},indent=2))
    if optimize.returncode!=0 and not absent:raise SystemExit('Unexpected optimization failure; see report')

if __name__=='__main__':main()
