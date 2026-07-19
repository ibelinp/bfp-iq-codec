"""
BFP vs. Block Scaling, measured.

Block scaling is the main alternative to BFP: same block structure, but each
block carries a fine LINEAR scale factor instead of a power-of-two exponent.
The finer step fills the mantissa range a little better, so it wins a dB or
two on quality. The catch is decode cost: block scaling needs a multiply per
sample, BFP needs a shift.

This script quantizes a few signals both ways and prints the SNR so you can see
the gap for yourself. Block scaling here uses an *idealized* full-precision
scale factor (a real fixed-point one would quantize it and give some of the
edge back), so these numbers are the best case for block scaling. Even
flattered, the gap to well-filled BFP is small.

Run:  python3 compare.py

Depends only on bfp.py (same folder) and the standard library.
"""

from __future__ import annotations

import cmath
import math
import random

from bfp import BfpFormat, decode_block, encode_block


def bfp_roundtrip(samples, bits: int, fill: str):
    """Encode with the BFP reference and decode back, per block."""
    fmt = BfpFormat(BLOCK, bits)
    out = []
    for i in range(0, len(samples), BLOCK):
        blk = samples[i:i + BLOCK]
        out.extend(decode_block(fmt, encode_block(fmt, blk, fill=fill)))
    return out


def bsc_roundtrip(samples, bits: int):
    """Block scaling: per-block linear scale = peak / mantissa_max (idealized,
    full-precision), then quantize the mantissas the same way BFP does. Decode
    is sample * scale (a multiply, vs BFP's shift)."""
    m_max = (1 << (bits - 1)) - 1
    lo, hi = -m_max - 1, m_max

    def rnd(x):
        return math.floor(x + 0.5) if x >= 0 else -math.floor(-x + 0.5)

    out = []
    for i in range(0, len(samples), BLOCK):
        blk = samples[i:i + BLOCK]
        peak = 0.0
        for s in blk:
            peak = max(peak, abs(s.real), abs(s.imag))
        if peak <= 0.0:
            out.extend([0j] * len(blk))
            continue
        scale = peak / m_max              # maps the block peak exactly to full scale
        inv = 1.0 / scale
        for s in blk:
            mi = max(lo, min(hi, rnd(s.real * inv)))
            mq = max(lo, min(hi, rnd(s.imag * inv)))
            out.append(complex(mi * scale, mq * scale))
    return out


def snr_db(orig, dec):
    sig = sum(abs(s) ** 2 for s in orig)
    err = sum(abs(a - b) ** 2 for a, b in zip(orig, dec))
    return 10 * math.log10(sig / err) if err > 0 else float("inf")


# ---- test signals (fs = 4 kHz baseband channel, like a decimated SSB channel)

FS = 4000.0
N = 4096
BLOCK = 256


def tone(freq, amp, n=N):
    return [amp * cmath.exp(2j * math.pi * freq / FS * i) for i in range(n)]


def noise(sigma, seed):
    r = random.Random(seed)
    return [complex(r.gauss(0, sigma), r.gauss(0, sigma)) for _ in range(N)]


def add(*sigs):
    return [sum(v) for v in zip(*sigs)]


SIGNALS = {
    # Low dynamic range: filled-band noise, fairly uniform envelope.
    "low-DR (band noise)": noise(0.18, seed=1),
    # High DR: one strong carrier, one carrier 45 dB down in the same block.
    "high-DR (strong+weak)": add(tone(863.0, 0.5), tone(1523.0, 0.5 * 10 ** (-45 / 20))),
    # High DR: two equal tones over a low noise floor.
    "high-DR (2-tone+noise)": add(tone(837.0, 0.35), tone(1123.0, 0.35), noise(0.01, seed=7)),
}


def main() -> None:
    print(f"\nBFP vs Block Scaling   fs={FS/1000:.0f} kHz  block={BLOCK}   (SNR dB, higher = better)")
    print("BFP-ceil = safe under-filled exponent;  BFP-tuned = round-fill exponent;")
    print("BSC = block scaling with an idealized (best-case) linear scale factor.\n")
    print(f"{'signal':<24}{'bits':>5}{'BFP-ceil':>10}{'BFP-tuned':>11}{'BSC':>8}{'BSC−tuned':>11}")
    for name, sig in SIGNALS.items():
        for bits in (4, 5, 6, 8):
            c = snr_db(sig, bfp_roundtrip(sig, bits, "ceil"))
            t = snr_db(sig, bfp_roundtrip(sig, bits, "round"))
            b = snr_db(sig, bsc_roundtrip(sig, bits))
            print(f"{name:<24}{bits:>5}{c:>10.1f}{t:>11.1f}{b:>8.1f}{b - t:>+11.1f}")
        print()
    print("Takeaway: against well-filled BFP (BFP-tuned), block scaling wins by only")
    print("about 1-3 dB at usable bit depths, and it's flattered here. For that it asks")
    print("a multiply per sample on decode instead of a shift. BFP is the better default;")
    print("block scaling earns its keep only on genuinely high-dynamic-range wideband IQ.")


if __name__ == "__main__":
    main()
