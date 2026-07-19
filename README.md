# bfp-iq

Block Floating Point compression for complex IQ streams. It's a small, fast,
loss-tolerant way to cut SDR IQ data roughly in half (or better) without an
entropy coder, without per-sample state, and without giving up the dynamic
range weak signals need. This repo explains the format in enough detail that
you can implement it in an afternoon, and ships a readable reference encoder
and decoder in Python.

I wrote this up because the technique kept coming up while building a web SDR —
the browser does the demodulation, so the server has to ship IQ, and shipping
raw IQ is expensive. BFP turned out to be the right tool, it's used in places
you'd expect it to be (5G fronthaul, radar front-ends), and yet there's almost
no plain-language description of it aimed at the SDR crowd. So here's one.

## The problem it solves

If you want a thin client (a phone, a browser tab) to demodulate any mode it
likes — SSB, NFM, AM, CW, whatever — you can't send it demodulated audio. You
have to send it IQ and let it do the DSP. The catch is that IQ is bulky. A
200 kHz FM channel as 16-bit complex samples is 800 kB/s. Multiply by a few
listeners and you're saturating links for no good reason.

The usual compressors don't fit:

- **gzip / FLAC / lossless audio codecs** are variable-rate and stateful. You
  can't predict the bitrate, and a dropped packet corrupts the decoder until
  the next resync point. Bad for real-time streaming over lossy transports.
- **Just send fewer bits** (say, 8-bit instead of 16-bit) works, but a fixed
  bit depth wastes range: when the signal is quiet, you're quantizing near the
  bottom of a scale sized for the loud case.
- **Server-side AGC then quantize** throws away the very thing you want to
  keep — the true relative signal levels, which matter for an S-meter and for
  letting the client run its own AGC per mode.

What you actually want for this job is a codec that is:

1. **Fixed rate per block.** No surprises, no content-dependent size.
2. **Cheap to decode.** One multiply (or one shift, in fixed point) per sample.
   No lookup tables, no arithmetic decoder, no branch-heavy inner loop.
3. **Stateless across blocks.** Lose a block, lose only that block. No resync.
4. **Adaptive in level.** Quiet signals should still get most of the mantissa
   precision, not just the low end of a fixed scale.

Block Floating Point hits all four.

## The idea

Ordinary floating point stores an exponent and a mantissa per number. That's
wasteful when a whole run of numbers sits at a similar magnitude — you're
paying for the exponent over and over. BFP amortizes it: take a block of N
complex samples, find one exponent that fits the whole block, and store just a
fixed-width mantissa per sample.

```
sample = mantissa × 2^exponent
```

The exponent tracks the block's overall level (like a gain that follows the
signal), but — and this is the important part — it is **reversible**. The
decoder multiplies back by `2^exponent` and recovers the original amplitude
exactly, up to the mantissa's rounding. It is emphatically *not* AGC. AGC is a
lossy gain decision made before the data is ever seen; the BFP exponent is
bookkeeping that the decoder undoes. If you keep that distinction straight,
everything else follows.

Because the exponent is shared, a block of N samples costs one exponent byte
plus N small mantissas, instead of N full floats.

## The wire format

One block is:

```
offset  size                       field
0       1 byte    (i8)             exponent   e, signed
1       ceil(N·b/8) bytes          I mantissas, N of them, b bits each
...     ceil(N·b/8) bytes          Q mantissas, N of them, b bits each
```

where `N` is the block size (samples per block) and `b` is the mantissa width
in bits, per component. Notes that matter for interop:

- **Planar, not interleaved.** All I mantissas, then all Q. (Interleaving works
  too; planar just packs a hair cleaner and vectorizes nicely.)
- **Mantissas are signed two's-complement, `b` bits wide, packed LSB-first.**
  Sample index `i` occupies bits `[i·b, (i+1)·b)` of the plane, low bits first.
  A value can straddle up to three bytes for `b ≤ 16`.
- **Exponent is a signed byte.** Range −128…127 is far more than any real
  signal needs; −128 is the sentinel for an all-zero block.

Sizes:

```
plane_bytes = ceil(N · b / 8)
block_bytes = 1 + 2 · plane_bytes
```

Worked example — the common voice-channel setting, N = 256, b = 5:

```
plane_bytes = ceil(256 · 5 / 8) = 160
block_bytes = 1 + 2·160        = 321 bytes  for 256 complex samples
            = 1.254 bytes per complex sample
```

Against 16-bit complex (4 bytes/sample) that's a 3.2× reduction, and against
8-bit complex (2 bytes/sample) it's still 1.6×, with the exponent buying back
dynamic range that a flat 8-bit scale doesn't have.

## Encoding

Two steps: pick the exponent, then quantize.

**Exponent (peak-based, `ceil`).** Find the block's peak magnitude, then choose
the smallest exponent such that the peak's mantissa doesn't overflow:

```
m_max    = 2^(b-1) − 1                       # largest positive mantissa
peak     = max over the block of max(|I|, |Q|)
exponent = ceil(log2(peak / m_max))          # (−128 if the block is all zeros)
```

This guarantees nothing clips. It costs a little resolution: the peak lands
somewhere between 50% and 100% of full mantissa range depending on where it
falls relative to a power of two, so on average you give away about 3 dB. See
"the fill-rule knob" below if you want that back.

**Quantize.** Scale each component down by the exponent and round to the
nearest integer:

```
scale     = 2^(−exponent)
mantissa  = round(sample · scale)            # clamp to [−m_max−1, m_max]
```

The clamp only ever fires on the rounding edge (a value rounding just past the
peak), which is why `ceil` is safe.

## Decoding

This is the whole point — decode is trivial and stateless:

```
scale  = 2^(exponent)
sample = mantissa · scale
```

One multiply per component in floating point, or a shift if you're working in
fixed point. No table, no predictor, no cross-block dependency. Drop a block on
the floor and you've lost exactly that block; the next one decodes on its own.
That independence is worth a lot on real networks and is the main thing you'd
give up by switching to an entropy coder.

## Choosing the mantissa width

`b` is a knob, and it's worth exposing it (in our streaming protocol the client
picks it per subscription). The peak SQNR near full scale is the textbook
uniform-quantizer figure, relative to the block's own peak:

```
SQNR ≈ 6.02·b + 1.76 dB
```

Reasonable starting points:

| b (bits) | ~SQNR | bytes/sample | Good for |
|---|---|---|---|
| 3 | ~20 dB | 0.875 | wide panorama / display, fidelity not critical |
| 4 | ~26 dB | 1.125 | wideband, RAW capture |
| 5 | ~32 dB | 1.25 | general voice: SSB, NFM, AM |
| 6 | ~38 dB | 1.5 | weak-signal work: CW, digital modes |
| 8 | ~50 dB | 2.0 | when you want the display floor to show the real filter skirts |
| 12 | ~74 dB | 3.0 | near-transparent for a 12-bit-ADC front end |

The 3…12 range covers everything from "just show me where the signals are" to
"don't touch my weak DX." Below 3 bits the mantissa can't hold a sign plus
useful magnitude; above 12 you're past most SDR ADCs anyway.

## Dynamic range: two different things people conflate

"Dynamic range" gets used for two things here, and it's worth separating them:

1. **Peak SQNR** near full scale — the `6.02·b + 1.76` figure above.
2. **Usable range of input levels** — how far the signal can drop and still be
   represented well. This is where BFP earns its keep: mantissa SQNR is
   relative to the *block's own peak*, not to some fixed system full-scale, so
   a weak signal gets full mantissa precision as long as it isn't sharing a
   block with something much stronger.

That caveat is real: two signals of very different strength in the *same
passband, in the same block* are still limited to the mantissa's SQNR relative
to each other. BFP tracks level over time; it doesn't give you simultaneous
same-block dynamic range. (Neither does a real ADC next to a strong nearby
signal — same SFDR limitation.)

### The one that bites people: quantization floor vs. filter floor

If you compute a spectrum from BFP-decoded IQ, the noise floor you *see* is
capped by the mantissa, independent of how good your receiver actually is.
Worked example measured against a live receiver: a wideband view showed a
signal at −110 dBFS and the channel filter's edge rolloff 20 dB down at
−130 dBFS. The narrowband IF spectrum, computed from 8-bit BFP IQ, showed the
same −110 signal but its edge sat at about −160 dBFS — which is exactly
`−110 − (6.02·8 + 1.76)`, the 8-bit SQNR floor below that block's peak.

That −160 isn't your filter's real stopband. It's the encoding's own
quantization noise becoming the visible limit once the true (deeper) filter
output drops below it. The takeaway: **mantissa depth caps how deep your
displayed noise floor can go.** If you want a spectrum display to reveal a
70–80 dB filter stopband rather than a quantization artifact, your mantissa
SQNR has to comfortably exceed that — which may call for more bits than
weak-signal *reception* alone would suggest. Reception quality and display
floor are two different budgets.

## The fill-rule knob (worth ~3 dB)

The `ceil` exponent is safe but under-fills the mantissa: the peak lands
between half and full range. You can recover most of that ~3 dB by choosing the
exponent so the peak sits near full-scale (round-to-nearest on the exponent, or
equivalently MSB-position based). This is what the O-RAN and radar-SoC
implementations do.

One caution, measured the hard way: naive exponent rounding can push near-peak
samples *past* full-scale and clip them, and at wide mantissas (8-bit) that
clipping noise can be worse than the under-fill it was trying to fix (we saw
38 dB drop to 31 dB). A safe tuned fill has to bound the clip rate — round only
when it won't clip more than a small fraction of samples — or fall back to
`ceil`. The reference here uses plain `ceil` for clarity; treat the tuned fill
as an encoder-side upgrade the decoder never needs to know about.

## Where this is actually used

BFP isn't exotic. It's the compression method in places where IQ throughput and
hardware cost both matter:

- **5G fronthaul (O-RAN).** The O-RAN WG4 CUS spec standardizes block floating
  point for IQ between radio and baseband units — shared exponent plus fixed
  mantissa, per 12-sample resource block. This is the large-scale industrial
  validation of the exact scheme described here. (O-RAN also defines block
  scaling and µ-law as alternatives; more on that below.)
- **Radar and FPGA front-ends.** An Oulu MSc thesis (Aouaneche, 2025)
  implements and compares BFP vs. block scaling on real captured radar IQ in
  FPGA hardware. On low-dynamic-range IQ the two are within ~1 dB PSNR, but BFP
  is roughly 3× cheaper in logic and 2–3× lower latency because its decode is a
  shift, not a multiply. For low-DR IQ, well-tuned BFP wins on cost at equal
  quality.
- **SDR streaming to thin clients.** The original motivation here: keep the
  server dumb (tune, filter, quantize, send) and let the browser demodulate. A
  200 kHz WFM channel at 5-bit BFP is ~300 kB/s instead of ~800 kB/s, and the
  client still gets honest levels for its S-meter.

For contrast, a few approaches other IQ-streaming systems take, and why they're
different:

- The simplest servers send **raw interleaved IQ** at reduced bit depth (8-bit
  is common) — the no-compression baseline. Fine, but a flat scale wastes range
  on quiet signals.
- Some servers **requantize to fewer bits per component with one global digital
  gain** — a single flat scale, no per-block exponent — and a few add an
  optional **adaptive arithmetic-coded tier** for maximum compression. Neither
  is block floating point. BFP is a strictly additive improvement over flat
  requantization (the shared exponent buys back the range a flat scale throws
  away), and it's cheaper and more loss-tolerant than an arithmetic-coded tier.
- Some **phase-coherent receivers** deliberately send full uncompressed 16-bit
  IQ and compress only the demodulated audio — the opposite tradeoff, chosen
  where sample precision mattered more than bandwidth.

## BFP vs. the alternatives, briefly

- **Block scaling** uses a fine linear scale factor (1–128) instead of a
  power-of-two exponent. It pulls ahead on high-peak-to-average data (high-order
  QAM, dense multi-carrier) where BFP's power-of-two step crushes small samples.
  It costs a multiply per sample to decode instead of a shift. Consider it only
  for a high-dynamic-range wideband tier, and only if a faithful
  (scale-quantized) benchmark on *your* signals shows it winning by enough to
  justify the decode cost.
- **µ-law / companding** is dominated by both on this kind of data. Skip it.
- **Adaptive arithmetic coding** (CABAC-like coding over Golomb-binarized
  residuals) compresses harder, but it's variable-rate, serial, and stateful —
  a corrupted byte desyncs the decoder until the next resync. That's the wrong
  tradeoff for fixed-rate, low-latency, loss-tolerant streaming. It's a
  legitimate "maximum compression, reliable transport, latency-tolerant" *extra
  tier* alongside BFP, not a replacement.

Recommendation for streaming IQ to thin clients: **well-tuned BFP as the
default.** Competitive quality, cheapest decode, independent blocks.

### Measured: BFP vs. block scaling

Block scaling is the alternative most worth taking seriously, so here are real
numbers. [`reference/compare.py`](reference/compare.py) quantizes a few signals
both ways and prints the SNR. It gives block scaling every advantage — an
idealized, full-precision linear scale factor that a real fixed-point
implementation wouldn't have — so these figures are the *best case* for block
scaling. The table below is the gap (block scaling minus well-filled BFP), in
dB; positive means block scaling is ahead:

| signal | 4-bit | 5-bit | 6-bit |
|---|---|---|---|
| low dynamic range (band noise) | −0.9 | −0.4 | +1.4 |
| high DR (strong + weak, 45 dB apart) | +1.7 | +1.5 | +0.5 |
| high DR (two tones + noise floor) | +1.9 | +2.3 | +2.8 |

So at the bit depths you'd actually stream (4–6), block scaling wins by roughly
**0 to 3 dB**, and on genuinely low-dynamic-range IQ it's a wash or BFP edges
ahead. That's the *flattered* margin. For it you pay a multiply per sample on
decode instead of a shift, plus the logic to quantize and carry a linear scale
factor. On the decimated, band-limited IQ that SDR channels actually produce —
which is low dynamic range by construction — it isn't worth it.

(One caveat the script also surfaces: at 8 bits the naive round-fill BFP can
clip and score *worse* than the safe `ceil` fill. That's the fill-rule gotcha
from earlier, not a block-scaling win. Use `ceil`, or a clip-bounded tuned
fill, at wide mantissas.)

Where block scaling does earn its place: high-peak-to-average wideband IQ
(dense multi-carrier, high-order QAM), where BFP's power-of-two step crushes the
small samples. If that's your data, benchmark a *faithful* (scale-quantized)
block scaling against tuned BFP on your own captures before committing to the
costlier decode.

## Reference implementation

[`reference/bfp.py`](reference/bfp.py) is a dependency-free Python encoder and
decoder that matches the wire format above byte-for-byte, plus a runnable demo
that round-trips a tone at several bit depths and prints the measured SQNR and
byte rate:

```
python3 reference/bfp.py
```

It's about 60 lines of actual logic. Porting it to C, Rust, JS/WASM, or an FPGA
is straightforward — the encoder is a peak scan plus a pack loop, and the
decoder is an unpack loop plus one multiply. If you build a port, the two
things to get exactly right for interop are the LSB-first bit packing and the
planar (all-I-then-all-Q) layout.

[`reference/compare.py`](reference/compare.py) is the BFP-vs-block-scaling
harness behind the numbers above. It also contains a short, readable block
scaling implementation if you want to see the alternative side by side:

```
python3 reference/compare.py
```

## License

MIT. See [LICENSE](LICENSE). Use it, ship it, port it. If it saves someone from
reinventing this or from reaching for a heavier codec than they need, it did its
job.
