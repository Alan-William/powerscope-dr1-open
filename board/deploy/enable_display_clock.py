"""Open the checked VTG clock gate, retaining compiled PLL coefficients.

Register definitions are from video_timing_generator_v01_e2dadf6740ca.sv:
0x1008 bit 0 is PLL lock; 0x100c bit 0 enables output and bit 8 resets PLL.
This service only writes 1 to the output-control register, never to PLL DRP.
"""
import argparse
import hashlib
import importlib.util
import json
import mmap
import os
from pathlib import Path
import struct
import time

_binder_spec = importlib.util.spec_from_file_location(
    'display_uio_contract', str(Path(__file__).with_name('bind_headless_uio.py')))
_binder = importlib.util.module_from_spec(_binder_spec)
_binder_spec.loader.exec_module(_binder)
design_contract = _binder.design_contract


def enable(manifest):
    if os.geteuid() != 0:
        raise RuntimeError('Root service required')
    design = design_contract(manifest)
    if design.get('hdmi_clock_mode') != 'compiled-pll-output-gate':
        raise RuntimeError('Expected a checked static HDMI clock contract')
    for name, key in [('BOOT.bin', 'boot_sha256'), ('system.dtb', 'dtb_sha256')]:
        if hashlib.sha256((Path('/boot/board')/name).read_bytes()).hexdigest() != design[key]:
            raise RuntimeError('Boot file differs from checked display contract')
    soc = Path('/sys/firmware/devicetree/base/soc')
    if (soc/'ranges').read_bytes() != b'':
        raise RuntimeError('Unexpected SoC address translation')
    for key in ('#address-cells', '#size-cells'):
        if (soc/key).read_bytes() != struct.pack('>I', 2):
            raise RuntimeError('Unexpected SoC address cell format')
    node = soc/'axi_dynclk@80101000'
    if ((node/'status').read_bytes() != b'disabled\0' or
            (node/'compatible').read_bytes() != b'anlogic,dynclk\0' or
            struct.unpack('>4I', (node/'reg').read_bytes()) != (0, 0x80101000, 0, 0x1000)):
        raise RuntimeError('Live clock node differs from checked hardware')
    if Path('/sys/bus/platform/devices/80101000.axi_dynclk/driver').exists():
        raise RuntimeError('Dynamic clock driver must not own this PLL')
    fd = os.open('/dev/mem', os.O_RDWR | os.O_SYNC)
    try:
        with mmap.mmap(fd, 0x1000, flags=mmap.MAP_SHARED,
                       prot=mmap.PROT_READ | mmap.PROT_WRITE, offset=0x80101000) as mem:
            def read(offset):
                return struct.unpack_from('<I', mem, offset)[0]
            before = dict(drp_write=read(0), status=read(8), control=read(12))
            if before['drp_write'] != 0 or before['control'] not in (0, 1):
                raise RuntimeError('Clock registers changed after FPGA configuration')
            deadline = time.monotonic() + 1
            while not read(8) & 1 and time.monotonic() < deadline:
                time.sleep(.01)
            if not read(8) & 1:
                raise RuntimeError('Compiled VTG PLL is not locked')
            struct.pack_into('<I', mem, 12, 1)
            time.sleep(.01)
            after = dict(drp_write=read(0), status=read(8), control=read(12))
            if after['drp_write'] != 0 or after['control'] != 1 or not after['status'] & 1:
                raise RuntimeError('Display clock enable readback failed')
    finally:
        os.close(fd)
    return dict(status='static_pll_output_enabled', before=before, after=after,
                pll_coefficients_written=False, compiled_pixel_hz_ratio=[4900000000, 33])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--design-manifest', type=Path, required=True)
    print(json.dumps(enable(parser.parse_args().design_manifest)), flush=True)
