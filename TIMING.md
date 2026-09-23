# FT8 client timing

This document describes the calculations implemented in `sdr_ft8_client.py` and
`jt9.py`. All output fields ending in `_utc_ns` are integer **Unix nanoseconds**
(seconds since 1970-01-01 00:00:00 UTC multiplied by 1,000,000,000).

## Clock anchor and sample positions

The decoder captures `first_callback_utc_ns = time.time_ns()` on the first
nonempty `on_data` callback, before recording, calibration, or decoding.

- **Live reception:** this clock value becomes `start_utc_ns`, the estimated UTC
  of stream sample zero. Output `timestamp_source` is `"callback_arrival"`.
- **WAV replay:** an explicit `--start-utc` takes precedence; otherwise
  `start_utc_ns` comes from the corresponding `.json` sidecar, falling back to
  Unix epoch zero if no sidecar exists. Sidecar timestamp provenance is retained;
  a changed explicit override or the zero fallback uses `"explicit"`. The first
  callback clock is still captured but does not anchor the WAV.

The anchor is retained internally, not printed as a separate output field.
Subsequent sample timestamps are calculated from sample counts, not from later
callback times or the time decoding finishes. Replay speed does not affect WAV
recording timestamps.

Timing uses the nominal rate **12,000 samples/second**, with no resampling.
Startup accepts at most 0.001 Hz difference from that rate to accommodate server
reporting precision. For example, the queried Web-888 server announced
11999.999984 Hz; the pipeline uses integer 12000 for timing and WAV headers.

## Calibration and chunk boundaries

During calibration, all received samples remain in one accumulation buffer.
The first attempt occurs at 20 seconds; each failed attempt raises the target
by 5 seconds. On success, `result.mean` is used directly as the first chunk
boundary, in seconds relative to stream sample zero:

```python
first_chunk_sample = round(result.mean * 12000)
```

Samples before this boundary are discarded from the decoding buffer. Each
complete 15-second window (180,000 samples) is then enqueued, retaining the
incomplete tail. Stage 2 repeats this extraction after incoming blocks arrive.
Chunk indices are zero-based and do not count the discarded prefix:

```python
chunk_start_sample = first_chunk_sample + chunk_index * 180000
```

There is **no 0.5-second adjustment to result.mean in chunking**. An audio cut
point is not required to be an integral UTC slot boundary. The existing
calibrator uses power-based window selection and decoded timing to obtain its
mean; the client consumes that result without reinterpreting it.

Optional `--record` saves every incoming sample, including the discarded prefix
and final tail. Its sample zero is the original stream sample zero. The UTC
anchor is saved in a JSON sidecar rather than embedded in the WAV header.

## Recording metadata sidecar

`--record capture.wav` creates `capture.json` with metadata such as:

```json
{
  "format_version": 1,
  "sample_rate": 12000,
  "start_utc_ns": 1790122829215688498,
  "timestamp_source": "callback_arrival"
}
```

`start_utc_ns` is the sample-zero recording anchor, not the first calibrated
chunk time or the replay clock. For a live recording it is set on the first
nonempty callback, before processing the audio. Before any live audio arrives,
the sidecar contains `null` for this unknown timestamp. Such a sidecar cannot
supply a replay anchor; the loader reports an error rather than inventing one.

`-f capture.wav` automatically loads `capture.json` if present. A sidecar must
contain an integer `start_utc_ns`; malformed metadata is an error. With no
sidecar, the existing epoch-zero default applies.

`-f capture.wav --record copy.wav` preserves the input JSON bytes exactly,
including unknown fields and formatting, in `copy.json`. It does not replace
the original anchor with the current replay clock. An explicit `--start-utc`
that differs from the sidecar changes `start_utc_ns` and sets provenance to
`explicit` in the output metadata; other metadata fields are retained. The
input sidecar is unchanged. Replay without an input sidecar creates a new
sidecar using its selected anchor (zero unless overridden).

## Detection timestamp calculations

FT8's nominal signal start is **0.5 seconds after the slot boundary**. The `jt9`
command reports DT relative to that nominal start; DT does not include the
0.5 seconds. Consequently:

- `DT = 0.0`: signal starts about 0.5 seconds into the decoded window.
- `DT = -0.5`: signal starts about zero seconds into the decoded window.
- `DT = +0.2`: signal starts about 0.7 seconds into the decoded window.

Define the following quantities, all in nanoseconds except `dt` and `w`:

```python
N = 1_000_000_000
P = 15 * N
T0 = start_utc_ns
S = chunk_start_sample
w = record.window_start_seconds
dt = record.dt_seconds

# Rounded sample-count offset; retain integer nanosecond arithmetic.
C = T0 + (S * N + 12000 // 2) // 12000

# Estimated received slot time: detected signal start minus nominal 0.5 s.
U = C + round((w + dt) * N)

# Estimated actual signal start in the recording.
R = U + 500_000_000

# Nearest integral 15-second UTC slot; exact midpoint ties go later.
Q = ((U + P // 2) // P) * P

# Signed timing residual in seconds.
delay_seconds = (U - Q) / N
```

`U` is an intermediate value, not a separate output field. Equivalently:

```python
delay_seconds = (recording_utc_ns - 500_000_000 - calibrated_utc_ns) / 1e9
```

Ideal received timing gives zero delay. Positive values mean late relative to
the assigned slot; negative values mean early. This is not elapsed time spent
waiting for the chunk or running the decoder.

## Output fields

| Field | Meaning |
| --- | --- |
| `chunk_index` | Zero-based index of the complete chunk sent downstream. Multiple detections can share a chunk. |
| `chunk_start_sample` | Absolute sample index of the chunk's first sample in the original stream, including any discarded prefix. |
| `chunk_start_utc_ns` | `C`: estimated UTC of that first sample, derived from the anchor and sample count. |
| `recording_utc_ns` | `R`: estimated UTC of the detected **signal start**, including the nominal 0.5-second delay. |
| `calibrated_utc_ns` | `Q`: estimated slot time rounded to a 15-second UTC boundary. It labels the slot, not the signal start. |
| `delay_seconds` | `(U - Q) / 1e9`: signed received slot-time residual, excluding the nominal 0.5 seconds. |
| `timestamp_source` | Anchor provenance: `callback_arrival` for an original live arrival clock, or `explicit` for a supplied anchor/default zero. Replay retains sidecar provenance. |
| `decode.utc` | Synthetic decoder label `"000000"` from the temporary WAV filename. **Not a real UTC timestamp.** |
| `decode.dt_seconds` | `jt9` DT relative to the nominal 0.5-second signal start, reported to 0.1 seconds. |
| `decode.window_start_seconds` | Decoded window offset within the audio passed to the wrapper. Zero for this client's direct per-chunk `jt9.decode()` calls. |
| `decode.snr_db` | Received signal-to-noise ratio in dB. |
| `decode.frequency_hz` | Audio frequency in Hz, not the receiver's RF dial frequency. |
| `decode.mode` | Decoder mode marker; `~` denotes FT8. |
| `decode.message` | Decoded message text, preserved as returned. |

## Worked example

```json
{"chunk_index": 2, "chunk_start_sample": 378840, "chunk_start_utc_ns": 1790122860785688498, "recording_utc_ns": 1790122860785688498, "calibrated_utc_ns": 1790122860000000000, "delay_seconds": 0.285688498, "timestamp_source": "callback_arrival", "decode": {"utc": "000000", "snr_db": 6, "dt_seconds": -0.5, "frequency_hz": 729, "mode": "~", "message": "N3UO KI5OS RRR", "window_start_seconds": 0.0}}
```

This is a detection from the third chunk. Its first sample is
`378840 / 12000 = 31.57` seconds after stream sample zero. The first chunk began
at sample `378840 - 2 * 180000 = 18840`, or 1.57 seconds into the stream.

The inferred original live clock anchor is:

```text
1790122860785688498 - 31,570,000,000
= 1790122829215688498 ns
= 2026-09-23T00:20:29.215688498Z
```

The detection calculations are:

| Quantity | UTC |
| --- | --- |
| Chunk start `C` | `2026-09-23T00:21:00.785688498Z` |
| Signal start `R = C + (-0.5 + 0.5) s` | `2026-09-23T00:21:00.785688498Z` |
| Estimated received slot time `U = C - 0.5 s` | `2026-09-23T00:21:00.285688498Z` |
| Rounded slot `Q` | `2026-09-23T00:21:00.000000000Z` |

`recording_utc_ns` equals `chunk_start_utc_ns` here because DT is -0.5: the
estimated signal begins at the start of the chunk. The delay is:

```text
(1790122860785688498 - 500000000 - 1790122860000000000) / 1e9
= +0.285688498 seconds
```

The signal is estimated to arrive about **285.7 ms late** relative to the
nominal signal start at `00:21:00.500000000Z`. The delay is not 785.7 ms;
the nominal half-second has already been excluded.

## Interpretation and precision

The live anchor is the local clock when the first block reaches `on_data`, used
as an estimate of sample-zero UTC. It is not a server capture timestamp and is
not corrected for the first block's duration, network buffering, or transport
delay. Clock error, upstream processing, transmitter timing, propagation, and
sample-rate error can therefore contribute to the residual.

Because the slot is selected by nearest-boundary rounding, delay is confined to
`[-7.5, +7.5)` seconds. A whole-slot timing error can be hidden by assignment to a
different slot. This value alone cannot measure arbitrary end-to-end latency.
Calibration, queue waiting, decoding, and printing do not directly add elapsed
time to the calculated timestamp. They may delay when a result becomes visible.

Nanosecond storage preserves arithmetic and the clock anchor; it does not imply
nanosecond signal-timing accuracy. Sample spacing is approximately 83.33
microseconds, and `jt9` reports DT to 0.1 seconds. WAV timestamps and delays are
relative to the supplied anchor; default epoch-zero timestamps are not the
historical recording time unless that anchor is appropriate. Sidecar replay
preserves the original recording anchor automatically.
