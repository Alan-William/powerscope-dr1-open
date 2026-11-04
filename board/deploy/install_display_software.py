"""Install the hash-pinned waveform metadata update and native HDMI display.

The accepted model, worker and original release remain byte-for-byte intact.
New runtime files have their own content identity; no uncommitted snapshot is
presented as an existing Git revision. Run only after the HDMI boot is verified.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess

STAGE = Path('/home/uisrc/powerscope-dr1-tests/20261104-hdmi-display/display-update')
BASE_REVISION = 'c6c0fe5b987d677cb09ba0f04816d7bacfbdce31'
BASE = Path('/opt/powerscope/releases')/BASE_REVISION
DROPIN = Path('/etc/systemd/system/powerscope-ps.service.d/20-hdmi-waveform.conf')
DISPLAY_UNIT = Path('/etc/systemd/system/powerscope-display.service')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_file(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open('rb') as src, target.open('xb') as dst:
        shutil.copyfileobj(src, dst)
    os.chmod(target, 0o644)
    if sha(source) != sha(target):
        raise RuntimeError('Installed copy changed: ' + str(target))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest-sha256', required=True)
    args = parser.parse_args()
    if os.geteuid() != 0 or not stat.S_ISCHR(Path('/dev/fb0').stat().st_mode):
        raise RuntimeError('Requires root and a registered framebuffer')
    if Path('/opt/powerscope/current').resolve(strict=True) != BASE:
        raise RuntimeError('Production base revision changed')
    old = json.loads((BASE/'release-manifest.json').read_text())
    if old['revision'] != BASE_REVISION or old['model']['id'] != 'sfaug-r1' or old['sensor_rules']['enabled'] is not False:
        raise RuntimeError('Unexpected production classifier')
    for name, digest in old['files'].items():
        if sha(BASE/name) != digest:
            raise RuntimeError('Base release changed: ' + name)
    manifest_path = STAGE/'update-manifest.json'
    if sha(manifest_path) != args.manifest_sha256:
        raise RuntimeError('Display update manifest changed')
    update = json.loads(manifest_path.read_text())
    if update.get('base_revision') != BASE_REVISION or update.get('model') != 'sfaug-r1' or update.get('rules_enabled') is not False:
        raise RuntimeError('Unexpected display update contract')
    for name, digest in update['files'].items():
        rel = Path(name)
        source = STAGE/rel
        if rel.is_absolute() or '..' in rel.parts or source.is_symlink() or sha(source) != digest:
            raise RuntimeError('Unsafe or changed update member: ' + name)
    current = Path('/opt/powerscope-display/current')
    if DROPIN.exists() or DISPLAY_UNIT.exists() or current.exists() or current.is_symlink():
        raise RuntimeError('Existing display installation requires review')
    contract = json.loads(Path('/usr/local/lib/powerscope/display-boot.json').read_text())
    if sha(Path('/boot/board/BOOT.bin')) != contract['files_sha256']['BOOT.bin']:
        raise RuntimeError('Display boot identity mismatch')
    subprocess.run(['systemctl', 'is-active', '--quiet', 'powerscope-uio.service'], check=True)
    tag = 'hdmi-20261104-' + args.manifest_sha256[:12]
    runtime = Path('/opt/powerscope/display-runtime')/tag
    display = Path('/opt/powerscope-display/releases')/tag
    runtime.mkdir(parents=True, exist_ok=False)
    display.mkdir(parents=True, exist_ok=False)
    for name in ('ps', 'protocol'):
        shutil.copytree(str(BASE/name), str(runtime/name), ignore=shutil.ignore_patterns('__pycache__'))
    for name in ('ps/app.py', 'ps/network_input.py'):
        # These are fresh copies, never hard links to the accepted release.
        shutil.copyfile(str(STAGE/name), str(runtime/name))
        if sha(runtime/name) != update['files'][name]:
            raise RuntimeError('Runtime overlay changed')
    for name in ('board/deploy/run_ps_replay.sh', 'board/deploy/run_production.sh'):
        copy_file(BASE/name, runtime/name)
    for name in ('assets', 'bin'):
        (runtime/name).symlink_to(BASE/name, target_is_directory=True)
    (runtime/'source-revision.txt').write_text(BASE_REVISION+'+'+tag+'\n')
    display_files = ('board_display/__init__.py', 'board_display/__main__.py', 'board_display/state.py',
                     'ps/network_input.py', 'protocol/labels.py', 'board/deploy/run_display.sh')
    for name in display_files:
        copy_file(STAGE/name, display/name)
    for dest in (runtime, display):
        copy_file(manifest_path, dest/'display-update-manifest.json')
    subprocess.run(['systemctl', 'stop', 'powerscope-ps.service'], check=True)
    DROPIN.parent.mkdir(parents=True, exist_ok=True)
    with DROPIN.open('x') as f:
        f.write('[Service]\nWorkingDirectory='+str(runtime)+'\nExecStart=\nExecStart=/bin/sh '+str(runtime/'board/deploy/run_production.sh')+'\n')
    os.chmod(DROPIN, 0o644)
    copy_file(STAGE/'board/deploy/powerscope-display.service', DISPLAY_UNIT)
    current.symlink_to(display, target_is_directory=True)
    result = dict(tag=tag, base_revision=BASE_REVISION, runtime=str(runtime), display=str(display),
                  model_and_worker_unchanged=True, manifest_sha256=args.manifest_sha256,
                  service_dropin_sha256=sha(DROPIN), display_unit_sha256=sha(DISPLAY_UNIT))
    Path('/var/backups/powerscope-hdmi-20261104/software-install.json').write_text(json.dumps(result,indent=2)+'\n')
    subprocess.run(['systemd-analyze', 'verify', str(DISPLAY_UNIT)], check=True)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', 'powerscope-display.service'], check=True)
    subprocess.run(['systemctl', 'start', 'powerscope-ps.service', 'powerscope-display.service'], check=True)
    print(json.dumps(dict(status='DISPLAY_SOFTWARE_START_REQUESTED', **result)), flush=True)


if __name__ == '__main__':
    main()
