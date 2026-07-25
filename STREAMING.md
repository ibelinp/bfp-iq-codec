# Streaming BFP over transports that don't delimit

**Optional.** Nothing here changes the block format. A stream that uses none of
it is byte-identical to a plain run of BFP blocks. Skip the page unless you hit
the specific problem in §1.

---

## 1. The problem

A run of BFP blocks has no framing. That is deliberate: blocks are fixed-size
and independent, so a consumer that knows where one block starts knows where
every block starts, forever, by arithmetic. Nothing needs marking.

The catch is *knowing where one block starts*. Most transports tell you:

| transport | delimits? | what to do |
|---|---|---|
| File on disk | yes | Header gives the data offset; blocks follow at a fixed stride. Nothing to do. |
| WebSocket | yes | Messages are framed. Put whole blocks in a message. Nothing to do. |
| UDP / RTP | yes | Datagram boundaries. One or more whole blocks per datagram. |
| TCP under a length-prefixed protocol | yes | Your framing already does it. |
| AXI-Stream with TLAST | yes | Assert TLAST at frame boundaries; the bus carries alignment out of band. |
| **Raw DMA into a ring, no TLAST** | **no** | **§3 or §4** |
| **Raw byte pipe, SERDES, UART** | **no** | **§3 or §4** |
| **A consumer joining a live stream mid-flight** | **no** | **§3 or §4** |

If your transport is in the top group, stop reading — in-band sync would be pure
cost. This page is for the bottom group, where a receiver picks up a byte stream
with no idea of its phase.

BFP payload is close to uniformly random, so there is nothing to lock onto
unaided. A misaligned receiver produces plausible-looking noise indefinitely.

## 2. Two options, and how to choose

| | §3 Inline delimiter | §4 Sync block |
|---|---|---|
| What it is | A few bytes inserted between blocks, periodically | One block slot per period, marked by a reserved exponent |
| Overhead at a 13 ms period | **0.012%** (0.9 kB/s of 7.5 MB/s) | 0.39% (29 kB/s) |
| Stride | Broken at each delimiter; two divisions to map an offset | Uniform throughout; one division |
| A decoder that ignores this page | **Desynced permanently** from the first delimiter | Works — renders each marker as `N` samples of silence |
| Validating lock | Needs to know where you are in the period | Any block boundary, no phase counter |
| Simplest to implement | **Yes** | Close behind |

**Choose §3 if both ends are yours.** On a greenfield FPGA-to-receiver link it is
cheaper, simpler, and the backward-compatibility column never comes up because
nothing else will ever read that stream. This is the common case and the one to
reach for by default.

**Choose §4 if the stream may reach a decoder that doesn't know about sync** —
you are extending an existing deployment, publishing a stream others consume, or
you want any receiver to be able to re-confirm alignment at any boundary without
tracking where it is. You are paying about 30× more, but 30× more than nothing
is still 0.39%.

Neither is wrong. §3 optimises bytes and simplicity; §4 optimises
gracefully-degrading interop.

## 3. Inline delimiter

Insert a fixed header before every run of `K` data blocks. All fields
little-endian.

```
off  size  field            notes
0    4     magic            "BQS1" = 0x42 0x51 0x53 0x31
4    4     sequence         u32, +1 per delimiter; wraps
8    1     mantissa_bits    b — a receiver joining mid-stream learns the geometry
9    1     flags            bit 0 = real-valued input; others reserved, zero
10   2     block_size       N
12   2     run_length       K, data blocks until the next delimiter
14   2     reserved         zero
```

16 bytes, then `K × block_bytes` of data blocks, then the next delimiter.

**Acquiring lock:** scan for the magic, read `N` and `b`, compute
`block_bytes`, and confirm a second delimiter at `+16 + K × block_bytes` with
`sequence + 1`. The confirmation matters — a 32-bit magic false-triggers on
random payload roughly once per 4 GB, which at 7.5 MB/s is once every ten
minutes.

**Mapping an offset to a block** costs one extra division: find the period, then
subtract the 16-byte header. If you size a DMA transfer to exactly one period,
every completion lands on a delimiter and this disappears entirely.

## 4. Sync block

### 4.1 The marker

Exponent `-128` (`0x80`) is the all-zero-block sentinel: the block decodes to
silence. A sync block reuses it, and is told apart from a genuine zero block by
a magic in the mantissa plane — which for a real zero block is all zeros.

That is what makes this variant safe to introduce into an existing stream. A
decoder that knows nothing about sync blocks renders each one as `N` samples of
silence and carries on.

Note what *not* to do: a reserved **positive** exponent decodes as
`mantissa × 2^127` ≈ 3.4e38, and the first thing downstream that squares it
produces `inf`, then `NaN`. The whole reason `0x80` works is that every existing
decoder already treats it as silence.

### 4.2 Layout

A sync block occupies exactly one block slot — `block_bytes` bytes, in sequence
with the data blocks — so the stride stays uniform.

```
off  size  field            notes
0    1     0x80             exponent position: the zero-block sentinel
1    4     magic            "BQS1" = 0x42 0x51 0x53 0x31
5    1     version          1
6    1     mantissa_bits    b
7    1     flags            bit 0 = real-valued input; others reserved, zero
8    2     block_size       N
10   2     sync_period      M, blocks per period counting this one
12   4     sequence         u32, +1 per sync block; wraps
16   8     sample_rate_hz   f64, 0 = unknown
24   8     center_freq_hz   f64, 0 = unknown
32   8     tx_time_unix_ns  i64 UTC of the next block's first sample, 0 = unknown
40   ...   zero             padding to block_bytes, MUST be zero
```

Requires `block_bytes >= 40`. Every realistic streaming geometry clears it
(`N = 64, b = 4` is 65 bytes); below that, use §3.

In a stream blocks are contiguous — stride equals `block_bytes`. The padded
stride a `.biq` file may use is a file-layout convenience with no meaning on a
wire.

### 4.3 Acquiring lock

1. Scan for a byte equal to `0x80`. On mantissa data that hits about 1 byte in
   256 — a cheap filter, not a decision.
2. Check the magic at `+1`. A genuine zero block has zeros there, so real data
   cannot collide.
3. Read `N`, `b`, `flags`; compute `block_bytes`.
4. Confirm a second sync block at `+ M × block_bytes` with `sequence + 1`.
5. Locked. The `M - 1` blocks between markers are data.

**Losing lock.** Keep decoding when an expected marker is missing — a dropped
block is not a resync event — and return to scanning after a few consecutive
misses. Because every block boundary is independently testable, verifying lock
needs no phase counter.

## 5. Overhead

Both schemes cost a fixed number of bytes per period; the percentage is
independent of `b` and `N`, while the *interval* depends on both.

Figures below at `b = 6`, `N = 256` (385-byte blocks), 5 Msps — a 7.52 MB/s
stream. "Period" is the samples between markers.

| period (blocks) | interval | §3 delimiter | | §4 sync block | |
|---|---|---|---|---|---|
| | | overhead | added rate | overhead | added rate |
| 32 | 1.6 ms | 0.130% | 9.8 kB/s | 3.23% | 243 kB/s |
| 64 | 3.3 ms | 0.065% | 4.9 kB/s | 1.59% | 119 kB/s |
| 128 | 6.6 ms | 0.032% | 2.4 kB/s | 0.79% | 59 kB/s |
| **256** | **13.1 ms** | **0.016%** | **1.2 kB/s** | **0.39%** | **29 kB/s** |
| 512 | 26.2 ms | 0.008% | 0.6 kB/s | 0.20% | 15 kB/s |
| 1024 | 52.4 ms | 0.004% | 0.3 kB/s | 0.098% | 7.4 kB/s |

Exactly: §3 is `16 / (16 + K × block_bytes)`, §4 is `1 / (M - 1)`.

Worst-case acquisition is two periods either way — 26 ms at a period of 256.

**A period of 256 blocks is the recommended default for both.** At `N = 256`
that is a marker every 13 ms, locked in 26 ms, and the cost is somewhere between
negligible and invisible. Longer periods if the link is byte-tight; shorter if
you re-acquire often enough for lock time to matter.

Interval scales with `N` and inversely with sample rate; the percentages do not.
At `N = 1024` every interval above is 4× longer for the same overhead.

## 6. Compatibility

| | decoder that ignores this page | sync-aware decoder |
|---|---|---|
| no sync at all | works, unchanged | works; needs alignment out of band, as today |
| §3 inline delimiter | **desynced from the first delimiter** | works; self-syncing |
| §4 sync block | works; `N` samples of silence per period | works; self-syncing |

The top-left cell is why this is an optional page rather than a version bump:
not using either scheme costs nothing and changes nothing.

The §3 row is the honest cost of the cheaper option. It is not a defect — on a
link where both ends are yours, no decoder that ignores this page will ever see
the stream. It just means §3 is a *different stream format*, where §4 is an
*extension of the existing one*.

## 7. One normative requirement on the encoder (§4 only)

For the magic to be collision-free, **an all-zero block's mantissa planes MUST
be all zeros.** True of any straightforward implementation — if the peak is zero
you emit the sentinel and have nothing to pack — but §4 makes it load-bearing
rather than incidental, so encoders MUST guarantee it explicitly rather than
arriving at it via whatever their quantiser happens to do with zero.

## 8. Appendix: locking with no marker at all

You can synchronise on nothing, at zero cost in bytes.

Real exponents vary slowly and cluster in a narrow range — a few tens of codes
out of 256. Read the stream at each of the `block_bytes` candidate phases and
score the byte that would be the exponent over a window of blocks. The correct
phase shows a tight, smoothly-moving distribution; every wrong phase shows
mantissa bytes, near-uniform over the full range. Usually unambiguous within a
few dozen blocks.

Two failure modes:

- **Silence is degenerate.** An all-zero-block stream is zeros nearly
  everywhere, so no phase is distinguishable. It self-corrects when signal
  returns, but a receiver starting during a dead band sits unlocked.
- **It is inference, not proof**, and re-lock after a slip takes a window rather
  than one marker.

Reasonable for "the DMA is aligned after reset and I want a sanity check." Not a
substitute for §3 or §4 on a lossy link.

## 9. Recording a synced stream

A recorder writing a `.biq` file **strips the markers**, whichever scheme is in
use. They are transport scaffolding, the file header already carries everything
the descriptor was saying, and leaving them in would corrupt the sample-index
arithmetic that makes seeking O(1).

Exactly what SpectraFlux does with the IC-R8600's `0x8000` markers, one layer up.
