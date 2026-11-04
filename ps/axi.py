"""Fail-closed UIO mailbox adapter. Preview is the default and never maps MMIO."""

import ctypes
import fcntl
import mmap
import os
from pathlib import Path
import re
import stat
from protocol.mailbox import MailboxClient


class UioRegisters:
    def __init__(self, config, library):
        required = (
            "device",
            "expected_name",
            "expected_map0_addr",
            "expected_map0_size",
            "design_source",
        )
        if config.get("verified_design") is not True or any(
            not config.get(k) for k in required
        ):
            raise ValueError(
                "UIO requires a completed, verified teammate address contract"
            )
        device = config["device"]
        if not re.fullmatch(r"/dev/uio[0-9]+", device):
            raise ValueError("Only /dev/uioN is allowed")
        sysfs = Path("/sys/class/uio") / Path(device).name
        addr = int((sysfs / "maps/map0/addr").read_text().strip(), 0)
        size = int((sysfs / "maps/map0/size").read_text().strip(), 0)
        offset = int((sysfs / "maps/map0/offset").read_text().strip(), 0)
        if (
            (sysfs / "name").read_text().strip() != config["expected_name"]
            or addr != int(config["expected_map0_addr"], 0)
            or size != int(config["expected_map0_size"], 0)
            or offset != 0
            or size < 0x34
        ):
            raise ValueError("UIO identity/map does not match the hardware contract")
        self.lib = ctypes.CDLL(str(Path(library).resolve()))
        self.lib.ps_mmio_read.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self.lib.ps_mmio_read.restype = ctypes.c_uint32
        self.lib.ps_mmio_write.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        self.lib.ps_mmio_write.restype = None
        self.lib.ps_mmio_barrier.argtypes = []
        self.lib.ps_mmio_barrier.restype = None
        self.fd = os.open(device, os.O_RDWR | os.O_SYNC | os.O_CLOEXEC)
        try:
            if not stat.S_ISCHR(os.fstat(self.fd).st_mode):
                raise ValueError("Not a character device")
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.mapping = mmap.mmap(
                self.fd,
                size,
                flags=mmap.MAP_SHARED,
                prot=mmap.PROT_READ | mmap.PROT_WRITE,
            )
            self.address = ctypes.addressof(ctypes.c_char.from_buffer(self.mapping))
        except Exception:
            os.close(self.fd)
            raise

    def read(self, offset):
        if offset % 4 or not 0 <= offset <= 0x30:
            raise ValueError("Invalid register read")
        return self.lib.ps_mmio_read(self.address, offset)

    def write(self, offset, value):
        if offset not in (8, 12, 16, 20, 24, 28) or not 0 <= value <= 0xFFFFFFFF:
            raise ValueError("Invalid register write")
        self.lib.ps_mmio_write(self.address, offset, value)

    def barrier(self):
        self.lib.ps_mmio_barrier()

    def close(self):
        self.mapping.close()
        os.close(self.fd)


class Publisher:
    def __init__(self, config=None, library=None):
        self.io = UioRegisters(config, library) if config else None
        self.client = MailboxClient(self.io) if self.io else None

    def publish(self, p):
        if self.client:
            return dict(mode="uio", physical=True, **self.client.commit(p))
        return {
            "mode": "preview",
            "physical": False,
            "ack_token": None,
            "snapshot": None,
        }

    def close(self):
        if self.io:
            self.io.close()
