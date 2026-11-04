"""PSW1 codec for the verified PS -> PL -> PS sample transport.

ABI 1 result mailbox remains unchanged. The codec assigns no physical address;
ps.sample_transport enables returned Q15 input only with a verified UIO map.
"""

import math
import struct
import zlib

HEADER = struct.Struct("<8I")
MAGIC = int.from_bytes(b"PSW1", "little")
SAMPLE_COUNT = 1500
FRAME_BYTES = 32 + SAMPLE_COUNT * 2


def encode(sequence, samples):
    if type(sequence) is not int or not 0 <= sequence <= 0xFFFFFFFF:
        raise ValueError("Sequence must be uint32")
    if len(samples) != SAMPLE_COUNT or any(
        not math.isfinite(x) or abs(x) > 1 for x in samples
    ):
        raise ValueError("Expected normalized A500/B500/C500")
    payload = struct.pack("<1500h", *(round(x * 32767) for x in samples))
    prefix = struct.pack("<7I", MAGIC, 1, sequence, 1, 20000, (500 << 16) | 3, 3000)
    return prefix + struct.pack("<I", zlib.crc32(prefix + payload)) + payload


def decode(record):
    if len(record) != FRAME_BYTES:
        raise ValueError("Waveform record must be 3032 bytes")
    magic, version, sequence, flags, rate, shape, size, crc = HEADER.unpack_from(record)
    if (magic, version, flags, rate, shape, size) != (
        MAGIC,
        1,
        1,
        20000,
        (500 << 16) | 3,
        3000,
    ):
        raise ValueError("Waveform header mismatch")
    if zlib.crc32(record[:28] + record[32:]) != crc:
        raise ValueError("Waveform CRC mismatch")
    values = struct.unpack_from("<1500h", record, 32)
    if -32768 in values:
        raise ValueError("Q15 -32768 is outside this transport contract")
    return sequence, [x / 32767 for x in values]
