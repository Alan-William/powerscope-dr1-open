"""Bind the two verified PL windows after boot/DT checks; never accesses MMIO.

Without a manifest the original 236df82 headless contract is unchanged. A
display build requires a root-owned boot manifest from prepare_pl_boot.py.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import struct
import time


STAGE = Path('/home/uisrc/powerscope-dr1-tests/20261101-pl-headless-236df82')
BOOT_SHA = '0d0bd56ab9de02409a5f5b89d63b74ad13e0aa16a84ebfb18266f23330933fbe'
DTB_SHA = '6dfa8303f5b06f101414adfae5597edd9e37002b15ee5476d2ae87e62f4dbf40'


def timing_coverage_accepted(build):
    coverage = build.get('timing_metrics', {}).get('sta_coverage_percent')
    if coverage == 100:
        return True
    # Reviewed raw PLL lock / I2C control arcs in this exact routed image only.
    # Full review and graph inventory: reports/hdmi-timing-review-20261104.json.
    return (coverage == 99.97 and build.get('profile') == 'display'
            and build.get('bit_sha256') == 'd74b3fbe6c192150947a9dfbac1c38be710b1859f153515e8e2386be7238d5e2'
            and build.get('timing_review_sha256') == '4b37f1e1c0fd26b52939ab085fdf98a7ab021b7528952d33e9352fc17674b173')


def design_contract(path=None):
    if path is None:
        return dict(profile='headless', boot_sha256=BOOT_SHA, dtb_sha256=DTB_SHA,
                    previous_boot_id='ff732ad4-62fc-4ccd-8a25-008b6ae7726b',
                    source='236df82 routed image and live DT/platform/UIO map checks')
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_size > 65536:
        raise RuntimeError('Boot manifest must be a root-owned regular file without group/other write access')
    manifest = json.loads(path.read_text())
    build, tree = manifest.get('build', {}), manifest.get('device_tree_validation', {})
    metrics = build.get('timing_metrics', {})
    if (manifest.get('status') != 'prepared_offline_only' or build.get('profile') != 'display'
            or build.get('clock_plan') != 'shared-axi' or tree.get('profile') != 'display'
            or tree.get('hdmi_pixel_hz') != 148500000
            or metrics.get('setup_wns_ns', -1) < 0 or metrics.get('hold_wns_ns', -1) < 0
            or not timing_coverage_accepted(build)
            or manifest.get('fsbl_and_uboot_byte_identical') is not True
            or tree.get('all_other_node_properties_byte_identical') is not True):
        raise RuntimeError('Unverified display build manifest')
    files = manifest.get('files_sha256', {})
    clock_mode = tree.get('hdmi_clock_mode', 'vendor-dynamic')
    if clock_mode not in ('vendor-dynamic', 'compiled-pll-output-gate'):
        raise RuntimeError('Unknown HDMI clock initialization')
    if (clock_mode == 'compiled-pll-output-gate' and
            (tree.get('compiled_pixel_hz_ratio') != [4900000000, 33] or
             build.get('bit_sha256') != 'd74b3fbe6c192150947a9dfbac1c38be710b1859f153515e8e2386be7238d5e2')):
        raise RuntimeError('Static clock mode requires the reviewed bitstream')
    for name in ('BOOT.bin', 'system.dtb'):
        if not re.fullmatch(r'[0-9a-f]{64}', files.get(name, '')):
            raise RuntimeError('Invalid boot manifest hash')
    if files['system.dtb'] != tree.get('candidate_sha256'):
        raise RuntimeError('DTB validation hash mismatch')
    return dict(profile='display', boot_sha256=files['BOOT.bin'], dtb_sha256=files['system.dtb'],
                hdmi_clock_mode=clock_mode,
                previous_boot_id=manifest.get('previous_boot_id'),
                source='display image ' + files['BOOT.bin'][:12] + ' and live DT/platform/UIO map checks')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=STAGE / 'verified')
    parser.add_argument('--design-manifest', type=Path)
    args = parser.parse_args()
    if os.getuid() != 0:
        raise RuntimeError('Root console required')
    design = design_contract(args.design_manifest)
    boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if boot_id == design['previous_boot_id']:
        raise RuntimeError('Candidate has not been booted')
    for name, expected in [('BOOT.bin', design['boot_sha256']), ('system.dtb', design['dtb_sha256'])]:
        if hashlib.sha256((Path('/boot/board') / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError('Unexpected boot file: ' + name)
    dt = Path('/sys/firmware/devicetree/base/soc')
    if any((dt / key).read_bytes() != struct.pack('>I', 2)
           for key in ['#address-cells', '#size-cells']):
        raise RuntimeError('Unexpected SoC address cell format')
    if (dt / 'ranges').read_bytes() != b'':
        raise RuntimeError('Unexpected SoC address translation')
    for name in ['vdma@80200000', 'mipicam@80300000', 'vdmafb@80100000', 'axi_dynclk@80101000']:
        disabled = (design['profile'] == 'headless' or name.startswith('mipicam') or
                    (name.startswith('axi_dynclk') and design.get('hdmi_clock_mode') == 'compiled-pll-output-gate'))
        expected = b'disabled' if disabled else b'okay'
        if (dt / name / 'status').read_bytes().rstrip(b'\0') != expected:
            raise RuntimeError('Video node differs from checked profile: ' + name)
    if design['profile'] == 'display':
        for name in ('axi_dynclk@80101000/expclk', 'vdmafb@80100000/display-timings/timing1/clock-frequency'):
            if (dt / name).read_bytes() != struct.pack('>I', 148500000):
                raise RuntimeError('HDMI runtime pixel clock differs from routed design')
    driver = Path('/sys/bus/platform/drivers/uio_pdrv_genirq')
    if not driver.is_dir():
        raise RuntimeError('Current kernel generic UIO driver is absent')
    account = pwd.getpwnam('uisrc')
    output = args.output
    if output.is_symlink():
        raise RuntimeError('Refusing a symlink configuration directory')
    output.mkdir(mode=0o755, exist_ok=True)
    os.chown(output, 0, 0)
    os.chmod(output, 0o755)
    results = []
    for short, name, addr, size in [('axi', 'powerscope_mailbox', 0x80400000, 0x1000),
                                    ('waveform', 'powerscope_waveform', 0x80401000, 0x2000)]:
        node = dt / (name.replace('_', '-') + '@%x' % addr)
        if (node / 'compatible').read_bytes() != b'generic-uio\0':
            raise RuntimeError('Unexpected compatible')
        if (node / 'linux,uio-name').read_bytes() != name.encode() + b'\0':
            raise RuntimeError('Unexpected UIO name')
        if struct.unpack('>4I', (node / 'reg').read_bytes()) != (0, addr, 0, size):
            raise RuntimeError('Unexpected live DT address')
        devices = [p for p in Path('/sys/bus/platform/devices').iterdir()
                   if (p / 'of_node').is_symlink() and (p / 'of_node').resolve() == node]
        if len(devices) != 1:
            raise RuntimeError('Expected one platform device for ' + name)
        device = devices[0]
        # This board's platform bus does not expose a PCI-style resource file.
        # The checked live DT has identity ranges; the driver-exported map is
        # independently checked below before any MMIO client can open it.
        bound = device / 'driver'
        if bound.is_symlink():
            if bound.resolve() != driver.resolve():
                raise RuntimeError('Refusing to unbind another driver')
        else:
            (device / 'driver_override').write_text('uio_pdrv_genirq\n')
            (driver / 'bind').write_text(device.name + '\n')
        if bound.resolve() != driver.resolve():
            raise RuntimeError('UIO binding failed')
        matches = []
        for _ in range(30):
            matches = [p for p in Path('/sys/class/uio').glob('uio*')
                       if (p / 'device').resolve() == device.resolve()]
            if len(matches) == 1 and (Path('/dev') / matches[0].name).exists():
                break
            time.sleep(.1)
        if len(matches) != 1:
            raise RuntimeError('Expected one UIO device')
        uio = matches[0]
        actual = {key: int((uio / 'maps/map0' / key).read_text(), 0)
                  for key in ['addr', 'size', 'offset']}
        if actual != dict(addr=addr, size=size, offset=0) or (uio / 'name').read_text().strip() != name:
            raise RuntimeError('UIO map differs from contract')
        dev = Path('/dev') / uio.name
        if not stat.S_ISCHR(dev.stat().st_mode):
            raise RuntimeError('UIO is not a character device')
        os.chown(dev, account.pw_uid, account.pw_gid)
        os.chmod(dev, 0o600)
        config = dict(verified_design=True, device=str(dev), expected_name=name,
                      expected_map0_addr=hex(addr), expected_map0_size=hex(size),
                      design_source=design['source'],
                      boot_id=boot_id, boot_sha256=design['boot_sha256'],
                      note='Address identity verified; functional acceptance is recorded separately.')
        path = output / (short + '-uio.json')
        path.write_text(json.dumps(config, indent=2) + '\n')
        os.chown(path, account.pw_uid, account.pw_gid)
        results.append(dict(config=config, platform_device=device.name, map=actual))
    path = output / 'binding.json'
    path.write_text(json.dumps(dict(status='bound_and_mapped', results=results,
                                    mmio_accessed=False), indent=2) + '\n')
    os.chown(path, account.pw_uid, account.pw_gid)
    print(json.dumps(dict(status='bound_and_mapped', results=results, mmio_accessed=False)))


if __name__ == '__main__':
    main()
