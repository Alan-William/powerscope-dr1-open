"""Install or restore the paired, timing-checked DR1 HDMI boot files.

Run from the root console after copying a prepared candidate to the explicit
task staging directory. Never flashes, formats, hot-loads PL or reboots. The
verified pre-restore files remain available on the root filesystem.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

STAGE = Path('/home/uisrc/powerscope-dr1-tests/20261104-hdmi-display')
BACKUP = Path('/var/backups/powerscope-hdmi-20261104/pre-restore')
BOOT = Path('/boot/board')
OLD = {'BOOT.bin': '0d0bd56ab9de02409a5f5b89d63b74ad13e0aa16a84ebfb18266f23330933fbe',
       'system.dtb': '6dfa8303f5b06f101414adfae5597edd9e37002b15ee5476d2ae87e62f4dbf40'}
BINDER = Path('/usr/local/lib/powerscope/bind_headless_uio.py')
UNIT = Path('/etc/systemd/system/powerscope-uio.service')
CONTRACT = Path('/usr/local/lib/powerscope/display-boot.json')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_pair(directory, hashes):
    for name in ('BOOT.bin', 'system.dtb'):
        path = directory/name
        if path.is_symlink() or sha(path) != hashes[name]:
            raise RuntimeError('Boot file hash mismatch: ' + str(path))


def atomic_copy(source, target, suffix):
    temporary = target.with_name(target.name + suffix)
    with source.open('rb') as src, temporary.open('xb') as dest:
        shutil.copyfileobj(src, dest)
        dest.flush()
        os.fsync(dest.fileno())
    if sha(source) != sha(temporary):
        raise RuntimeError('Copy verification failed: ' + str(target))
    os.replace(temporary, target)


def restore():
    check_pair(BACKUP/'boot', OLD)
    dropin = Path('/etc/systemd/system/powerscope-ps.service.d/20-hdmi-waveform.conf')
    if dropin.exists():
        software = json.loads((BACKUP.parent/'software-install.json').read_text())
        if sha(dropin) != software['service_dropin_sha256']:
            raise RuntimeError('Display software override changed; review before restore')
    display_unit = Path('/etc/systemd/system/powerscope-display.service')
    if display_unit.exists() and 'PowerScope HDMI current waveform' in display_unit.read_text():
        subprocess.run(['systemctl', 'disable', '--now', 'powerscope-display.service'], check=True)
    subprocess.run(['systemctl', 'stop', 'powerscope-ps.service'], check=True)
    if dropin.exists():
        disabled_dropin = BACKUP.parent/'20-hdmi-waveform.disabled.conf'
        if disabled_dropin.exists():
            raise RuntimeError('Previous disabled override requires review')
        dropin.rename(disabled_dropin)
    for name in ('system.dtb', 'BOOT.bin'):
        atomic_copy(BACKUP/'boot'/name, BOOT/name, '.powerscope-hdmi-restore')
    for target in (BINDER, UNIT):
        atomic_copy(BACKUP/'config'/target.name, target, '.powerscope-hdmi-restore')
        os.chmod(target, 0o644)
    check_pair(BOOT, OLD)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    os.sync()
    print('HDMI_RESTORE_READY_REBOOT_REQUIRED', flush=True)


def install(args):
    candidate = STAGE/'candidate'
    manifest_path = candidate/'manifest.json'
    if sha(manifest_path) != args.manifest_sha256 or sha(STAGE/'bind_headless_uio.py') != args.binder_sha256:
        raise RuntimeError('Staging manifest or binder changed')
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('build', {}).get('profile') != 'display':
        raise RuntimeError('Expected a display-profile manifest')
    import importlib.util
    spec = importlib.util.spec_from_file_location('display_binder', str(STAGE/'bind_headless_uio.py'))
    binder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(binder)
    metrics = manifest['build']['timing_metrics']
    if metrics['setup_wns_ns'] < 0 or metrics['hold_wns_ns'] < 0 or not binder.timing_coverage_accepted(manifest['build']):
        raise RuntimeError('Timing checks failed')
    check_pair(candidate, manifest['files_sha256'])
    check_pair(BOOT, OLD)
    check_pair(BACKUP/'boot', OLD)
    required_space = 2*sum((BOOT/name).stat().st_size for name in OLD) + 8*1024*1024
    if shutil.disk_usage(str(BOOT)).free < required_space:
        raise RuntimeError('Insufficient boot-partition space for both candidate and recovery copies')
    for target in (BINDER, UNIT):
        if sha(target) != sha(BACKUP/'config'/target.name):
            raise RuntimeError('Installed service changed since backup: ' + str(target))
    if CONTRACT.exists() or CONTRACT.is_symlink():
        raise RuntimeError('Existing display contract requires review')
    worker = subprocess.run(['pgrep', '-x', 'ps_npu_worker'], stdout=subprocess.PIPE)
    if worker.returncode == 0:
        raise RuntimeError('An NPU worker is running; stop the current input session first')
    subprocess.run(['systemctl', 'stop', 'powerscope-ps.service'], check=True)
    # Recovery copies are also readable directly from the FAT boot partition.
    for name in OLD:
        recovery = BOOT/(name+'.pre-hdmi')
        if recovery.exists():
            if sha(recovery) != OLD[name]:
                raise RuntimeError('Unexpected existing FAT recovery copy')
        else:
            atomic_copy(BACKUP/'boot'/name, recovery, '.prepare')
    # Prepare and verify both candidates before replacing either active name.
    temporaries = {}
    for name in ('system.dtb', 'BOOT.bin'):
        target = BOOT/(name+'.powerscope-hdmi-new')
        with (candidate/name).open('rb') as src, target.open('xb') as out:
            shutil.copyfileobj(src, out)
            out.flush()
            os.fsync(out.fileno())
        if sha(target) != manifest['files_sha256'][name]:
            raise RuntimeError('Candidate copy changed')
        temporaries[name] = target
    manifest['previous_boot_id'] = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    manifest['installed_from_manifest_sha256'] = args.manifest_sha256
    with CONTRACT.open('x') as out:
        json.dump(manifest, out, indent=2)
        out.write('\n')
    os.chmod(CONTRACT, 0o644)
    # Validate the protected contract using the exact binder that will run.
    binder.design_contract(CONTRACT)
    unit_text = UNIT.read_text()
    expected = 'ExecStart=/usr/bin/python3 -I /usr/local/lib/powerscope/bind_headless_uio.py --output /run/powerscope'
    if unit_text.count(expected) != 1:
        raise RuntimeError('Unexpected UIO service command')
    prepared_unit = STAGE/'powerscope-uio-display.service.prepared'
    # Keep the replacement root-owned even though its staging parent is writable.
    with prepared_unit.open('x') as out:
        out.write(unit_text.replace(expected, expected+' --design-manifest '+str(CONTRACT)))
    os.chmod(prepared_unit, 0o644)
    atomic_copy(STAGE/'bind_headless_uio.py', BINDER, '.powerscope-hdmi-new')
    atomic_copy(prepared_unit, UNIT, '.powerscope-hdmi-new')
    os.chmod(BINDER, 0o644)
    os.chmod(UNIT, 0o644)
    os.sync()
    for name in ('system.dtb', 'BOOT.bin'):
        os.replace(temporaries[name], BOOT/name)
    check_pair(BOOT, manifest['files_sha256'])
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    os.sync()
    print(json.dumps(dict(status='HDMI_INSTALL_READY_REBOOT_REQUIRED',
                          files_sha256=manifest['files_sha256'], backup=str(BACKUP))), flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('install', 'restore'))
    p.add_argument('--manifest-sha256')
    p.add_argument('--binder-sha256')
    args = p.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError('Root console required')
    mount = subprocess.check_output(['findmnt', '-n', '-o', 'SOURCE,FSTYPE', '--target', str(BOOT)], text=True).split()
    if mount != ['/dev/mmcblk0p1', 'vfat']:
        raise RuntimeError('Unexpected boot mount')
    if args.action == 'restore':
        restore()
    elif args.manifest_sha256 and args.binder_sha256:
        install(args)
    else:
        p.error('Install requires the two locally checked SHA256 values')


if __name__ == '__main__':
    main()
