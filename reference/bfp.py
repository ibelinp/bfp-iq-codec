"""
Block Floating Point (BFP) codec for complex IQ samples.

Reference implementation matching the wire format in ../README.md. Pure Python,
no dependencies. The point is clarity, not speed — port the shape of it to C /
Rust / WASM / an FPGA and it goes fast.

Block layout:
    [ i8 exponent ] [ N mantissas for I ] [ N mantissas for Q ]
Mantissas are signed two's-complement, `b` bits each, packed LSB-first, planar
(all I, then all Q). sample = mantissa * 2**exponent.

Run this file directly for a round-trip demo:  python3 bfp.py
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass


@dataclass(frozen=True)
class BfpFormat:
    block_size: int      # N complex samples per block
    mantissa_bits: int   # bits per component (I and Q); 3..12 typical, 2..16 valid

    def __post_init__(self):
        if self.block_size <= 0:
            raise ValueError("block_size must be positive")
        if not 2 <= self.mantissa_bits <= 16:
            raise ValueError("mantissa_bits must be in 2..16")

    @property
    def plane_bytes(self) -> int:
        return (self.block_size * self.mantissa_bits + 7) // 8

    @property
    def block_bytes(self) -> int:
        return 1 + 2 * self.plane_bytes

    @property
    def mantissa_max(self) -> int:
        return (1 << (self.mantissa_bits - 1)) - 1  # largest positive mantissa


def _round_half_away(x: float) -> int:
    # Round-to-nearest, ties away from zero (matches Rust f32::round).
    return math.floor(x + 0.5) if x >= 0 else -math.floor(-x + 0.5)


def _pack(plane: bytearray, idx: int, bits: int, value: int) -> None:
    raw = value & ((1 << bits) - 1)          # two's-complement, `bits` wide
    bitpos = idx * bits
    byte = bitpos // 8
    word = raw << (bitpos % 8)               # spans <= 3 bytes for bits <= 16
    while word:
        plane[byte] |= word & 0xFF
        word >>= 8
        byte += 1


def _unpack(plane: bytes, idx: int, bits: int) -> int:
    bitpos = idx * bits
    byte = bitpos // 8
    word = 0
    for k in range(3):
        if byte + k < len(plane):
            word |= plane[byte + k] << (8 * k)
    raw = (word >> (bitpos % 8)) & ((1 << bits) - 1)
    sign = 1 << (bits - 1)                    # sign-extend
    return (raw ^ sign) - sign


def encode_block(fmt: BfpFormat, samples, fill: str = "ceil") -> bytes:
    """Encode N complex samples (nominally within +-1.0) into one BFP block.

    fill selects the exponent rule (see README, "the fill-rule knob"):
      "ceil"  - smallest exponent that guarantees no clip. Safe; under-fills the
                mantissa by up to 6 dB (~3 dB on average).
      "round" - place the peak near full-scale to recover most of that ~3 dB.
                Can clip a few near-peak samples at wide mantissas, so the clamp
                below matters; at 8 bits it can end up worse than "ceil".
    """
    if len(samples) != fmt.block_size:
        raise ValueError("expected exactly block_size samples")

    peak = 0.0
    for s in samples:
        peak = max(peak, abs(s.real), abs(s.imag))

    m_max = fmt.mantissa_max
    if peak > 0.0:
        raw = math.log2(peak / m_max)
        e = math.ceil(raw) if fill == "ceil" else round(raw)
        exponent = max(-128, min(127, e))
    else:
        exponent = -128                       # all-zero block sentinel

    scale = 2.0 ** (-exponent)
    lo, hi = -m_max - 1, m_max
    i_plane = bytearray(fmt.plane_bytes)
    q_plane = bytearray(fmt.plane_bytes)
    for n, s in enumerate(samples):
        mi = max(lo, min(hi, _round_half_away(s.real * scale)))
        mq = max(lo, min(hi, _round_half_away(s.imag * scale)))
        _pack(i_plane, n, fmt.mantissa_bits, mi)
        _pack(q_plane, n, fmt.mantissa_bits, mq)

    return struct.pack("b", exponent) + bytes(i_plane) + bytes(q_plane)


def decode_block(fmt: BfpFormat, data: bytes) -> list[complex]:
    """Decode one BFP block back into N complex samples."""
    if len(data) != fmt.block_bytes:
        raise ValueError("wrong block length")

    exponent = struct.unpack_from("b", data, 0)[0]
    scale = 2.0 ** exponent
    i_plane = data[1:1 + fmt.plane_bytes]
    q_plane = data[1 + fmt.plane_bytes:]

    out = []
    for n in range(fmt.block_size):
        mi = _unpack(i_plane, n, fmt.mantissa_bits)
        mq = _unpack(q_plane, n, fmt.mantissa_bits)
        out.append(complex(mi * scale, mq * scale))
    return out


def _demo() -> None:
    import cmath

    n = 256
    # A moderate tone plus a weaker one, so the SQNR is realistic (not a single
    # full-scale sinusoid, which flatters any quantizer).
    samples = []
    for i in range(n):
        a = 0.6 * cmath.exp(2j * math.pi * 0.019 * i)
        b = 0.08 * cmath.exp(2j * math.pi * 0.071 * i)
        samples.append(a + b)

    sig = sum(abs(s) ** 2 for s in samples)
    print(f"{'bits':>4}  {'bytes/samp':>10}  {'SQNR (dB)':>10}")
    for bits in (3, 4, 5, 6, 8, 12):
        fmt = BfpFormat(n, bits)
        blob = encode_block(fmt, samples)
        back = decode_block(fmt, blob)
        err = sum(abs(a - c) ** 2 for a, c in zip(samples, back))
        sqnr = 10 * math.log10(sig / err) if err > 0 else float("inf")
        print(f"{bits:>4}  {fmt.block_bytes / n:>10.3f}  {sqnr:>10.1f}")


if __name__ == "__main__":
    _demo()
