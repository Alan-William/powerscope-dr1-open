"""Verified UIO sample buffer, enabled by the PS app's --waveform-config option."""
from ps.axi import UioRegisters
from protocol.waveform_mailbox import BUFFER, FRAME_BYTES


class UioWaveformRegisters(UioRegisters):
    def __init__(self, config, library):
        if (config.get("expected_name") != "powerscope_waveform" or
                int(config.get("expected_map0_size") or "0", 0) < 0x2000):
            raise ValueError("Waveform UIO requires its own name and at least 0x2000 bytes")
        super().__init__(config, library)

    def read(self, offset):
        if offset % 4 or not (offset in range(0, 0x20, 4) or offset == 0x24 or
                              BUFFER <= offset < BUFFER + FRAME_BYTES):
            raise ValueError("Invalid waveform read")
        return self.lib.ps_mmio_read(self.address, offset)

    def write(self, offset, value):
        if (offset % 4 or not (offset in (8, 12, 20, 32) or BUFFER <= offset < BUFFER + FRAME_BYTES)
                or type(value) is not int or not 0 <= value <= 0xFFFFFFFF):
            raise ValueError("Invalid waveform write")
        self.lib.ps_mmio_write(self.address, offset, value)
