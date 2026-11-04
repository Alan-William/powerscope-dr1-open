"""Install a hash-pinned first PowerScope systemd release; never modifies boot files."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', type=Path, required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--previous-revision', help='Explicitly upgrade this installed revision while the PS service is stopped')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError('Run through the authorized root console')
    source = args.release.resolve(strict=True)
    allowed = Path('/home/uisrc/powerscope-dr1-tests/20261101-closeout')
    if allowed not in source.parents or source.is_symlink():
        raise RuntimeError('Unexpected staged release location')
    manifest_path = source / 'release-manifest.json'
    if sha(manifest_path) != args.manifest_sha256:
        raise RuntimeError('Release manifest hash mismatch')
    manifest = json.loads(manifest_path.read_text())
    revision = manifest['revision']
    if not re.fullmatch('[0-9a-f]{40}', revision):
        raise ValueError('Expected a committed revision')
    if (source / 'source-revision.txt').read_text().strip() != revision:
        raise RuntimeError('Revision file mismatch')
    for name, digest in manifest['files'].items():
        relative = Path(name)
        file = source / relative
        if relative.is_absolute() or '..' in relative.parts or file.is_symlink() or source not in file.resolve().parents:
            raise RuntimeError('Unsafe release member')
        if not file.is_file() or sha(file) != digest:
            raise RuntimeError('Release member mismatch: ' + name)
    required = {'ps/app.py', 'assets/manifest.json', 'bin/ps_npu_worker', 'bin/libpowerscope_mmio.so',
                'board/deploy/bind_headless_uio.py', 'board/deploy/run_production.sh',
                'board/deploy/powerscope-uio.service', 'board/deploy/powerscope-ps.service'}
    if not required.issubset(manifest['files']):
        raise RuntimeError('Incomplete release')
    units = Path('/etc/systemd/system')
    binder = Path('/usr/local/lib/powerscope/bind_headless_uio.py')
    current = Path('/opt/powerscope/current')
    installed_files = {units / name: 'board/deploy/' + name
                       for name in ['powerscope-uio.service', 'powerscope-ps.service']}
    installed_files[binder] = 'board/deploy/bind_headless_uio.py'
    if args.previous_revision:
        if not re.fullmatch('[0-9a-f]{40}', args.previous_revision):
            raise ValueError('Expected a full previous revision')
        previous = Path('/opt/powerscope/releases') / args.previous_revision
        if not current.is_symlink() or current.resolve(strict=True) != previous:
            raise RuntimeError('Installed revision differs from explicit upgrade target')
        old = json.loads((previous / 'release-manifest.json').read_text())
        if old['revision'] != args.previous_revision:
            raise RuntimeError('Previous manifest identity mismatch')
        for name, digest in old['files'].items():
            if sha(previous / name) != digest:
                raise RuntimeError('Previous release changed: ' + name)
        for path, name in installed_files.items():
            if path.is_symlink() or sha(path) != old['files'][name]:
                raise RuntimeError('Installed service file changed: ' + str(path))
        active = subprocess.check_output(['systemctl', 'show', 'powerscope-ps.service',
                                          '-p', 'ActiveState', '--value'], text=True).strip()
        if active != 'inactive':
            raise RuntimeError('Stop the current PS service before upgrading')
    else:
        for path in [*installed_files, current]:
            if path.exists() or path.is_symlink():
                raise RuntimeError('Existing install requires explicit upgrade review: ' + str(path))
    probe = socket.socket()
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(('127.0.0.1', 8765))
    finally:
        probe.close()
    destination = Path('/opt/powerscope/releases') / revision
    destination.mkdir(parents=True, exist_ok=False)
    for name, digest in manifest['files'].items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with (source / name).open('rb') as src, target.open('xb') as out:
            shutil.copyfileobj(src, out)
        os.chmod(target, 0o755 if name == 'bin/ps_npu_worker' else 0o644)
        if sha(target) != digest:
            raise RuntimeError('Installed member mismatch: ' + name)
    shutil.copyfile(str(manifest_path), str(destination / 'release-manifest.json'))
    binder.parent.mkdir(parents=True, exist_ok=True)
    for path, name in installed_files.items():
        temporary = path.with_name(path.name + '.new-' + revision)
        with (destination / name).open('rb') as src, temporary.open('xb') as out:
            shutil.copyfileobj(src, out)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    temporary = current.with_name('current.new-' + revision)
    temporary.symlink_to(destination, target_is_directory=True)
    os.replace(temporary, current)
    subprocess.run(['systemd-analyze', 'verify', str(units / 'powerscope-uio.service'),
                    str(units / 'powerscope-ps.service')], check=True)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', 'powerscope-uio.service', 'powerscope-ps.service'], check=True)
    subprocess.run(['systemctl', 'start', 'powerscope-ps.service'], check=True)
    print(json.dumps(dict(status='installed_and_start_requested', revision=revision,
                          destination=str(destination), manifest_sha256=args.manifest_sha256,
                          rollback='Disable both PowerScope units and start the retained 9b7cf87 release; boot files unchanged.')))


if __name__ == '__main__':
    main()
