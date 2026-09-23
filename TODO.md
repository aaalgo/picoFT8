# SDR audio sources and FT8 pipeline — session handoff

Updated: 2026-09-22.

## Current implementation: sdr_ft8_client.py

The client is implemented. This section supersedes the historical design notes
below, particularly asynchronous calibration, nearest-block chunking, optional
backlog decoding, and unresolved sample-rate handling.

```bash
python sdr_ft8_client.py -s web888.servehttp.com -p 8074
python sdr_ft8_client.py -f recording.wav
python sdr_ft8_client.py -f recording.wav --record out.wav
python sdr_ft8_client.py -f recording.wav --start-utc 2026-09-22T12:00:00Z
python -m unittest -v test_sdr_ft8_client.py
```

- Both sources feed FT8Decoder. Nominal 12000 Hz is required, allowing only
  0.001 Hz absolute reporting tolerance; no resampling. A live startup probe of
  web888.servehttp.com:8074 announced 11999.999984 Hz. KiwiRecorder rounds this
  to 12000 in the WAV header. Decoder timing and WAV output use integer 12000.
- Optional --record PATH.wav saves every received PCM block before calibration
  or chunking, including the discarded prefix and final incomplete tail. Output
  is mono 16-bit 12000 Hz WAV, finalized on EOF, stop, or error. Replay preserves
  audio samples exactly; ancillary input WAV metadata is not copied. The CLI
  rejects recording over its input file, including symbolic/hard-link aliases.
- Recording also writes a same-stem JSON sidecar containing integer
  start_utc_ns, sample_rate, format_version, and timestamp_source. WAV replay
  loads this anchor automatically; --start-utc overrides it and missing sidecars
  retain the epoch-zero default. Replay recording copies existing JSON bytes
  exactly unless the anchor is explicitly changed. Input WAV and JSON aliases
  are protected against overwrite. See TIMING.md for the schema and formulas.
- A single growable PCM buffer serves calibration and steady-state chunking.
- Calibration runs synchronously in on_data on the entire accumulated audio,
  first at 20 seconds, then with a target increased by 5 seconds on failure.
- On success, result.mean is used literally as the first chunk offset: discard
  its preceding samples, enqueue all full 15-second windows, retain the tail,
  and enter stage 2. No 0.5-second adjustment is applied to this offset.
- Stage 2 dispatches at >= 15 seconds; blocks are split at exact sample indices.
- One consumer thread calls jt9.decode and prints JSON lines. Diagnostic output
  goes to stderr. Calibration detections are not printed a second time.
- Chunk metadata includes index, absolute stream sample index, UTC nanoseconds,
  native sample rate, and independently owned PCM. Output includes the original
  DecodeRecord and recording/calibrated timestamps in Unix nanoseconds.
- Signal recording time = chunk UTC + DT + 0.5 seconds. Calibrated time rounds
  chunk UTC + DT to the nearest 15-second boundary, with midpoint ties later.
- Printed delay_seconds = (chunk UTC + DT) - calibrated slot UTC. It is the
  signed slot timing residual derived from the initial receiving clock and
  sample offsets, excluding the nominal 0.5 seconds and decoder processing time.
- Live sample-zero UTC uses the first nonempty callback arrival clock. WAV
  sample-zero UTC defaults to the JSON sidecar or epoch 0; --start-utc accepts Unix seconds or a
  timezone-qualified ISO timestamp. The first callback clock is also retained.
- Calibration has a configurable 300-second default limit. The bounded queue
  defaults to 32 windows: WAV waits for capacity; live overflow stops with an
  error. Worker failure stops the source and produces a nonzero exit status.
- End-of-stream drains complete queued windows and discards the tail. EOF
  before calibration succeeds reports failure. Each run uses a new decoder.
- jt9.InsufficientAudioError distinguishes a missing complete calibration
  window from other errors; the client retries only that condition or mean=None.

CLI help lists receiver tuning/connection options, WAV pacing/block size,
calibration initial/step/maximum durations, and queue size. Period remains fixed
at 15 seconds; uploads and automatic reconnect are not implemented.

Live reception and the impact of synchronous calibration on socket buffering
still require validation. Arrival timestamps are estimates, not server capture
timestamps.

Validation: fifteen deterministic pipeline tests pass, including exact sample
preservation, retry scheduling, slot rounding, queue pressure and worker failure.
Recording tests cover replay equality, finalization on stop/error, CLI wiring,
and protection against overwriting the replay input.
Sidecar tests cover exact JSON round trips, anchor restoration, explicit
overrides, invalid metadata, and sidecar alias protection.
A 45-second replay concatenated from three existing 12 kHz FT8 test recordings
also passed through the installed jt9 executable, producing four detections.

## Historical handoff (superseded where noted above)

## Goal and scope

Build a continuously running FT8 receiver using the existing KiwiClient library.
Receive demodulated USB audio, find the correct chunk timing by calibration,
then repeatedly decode chunks and eventually send decoded messages to the user's
own server. Support WAV replay through the same processing interface for testing.

Keep new application code OUTSIDE the kiwiclient repository. Workspace layout:

```text
/home/wdong/picoFT8/test
├── TODO.md               # This handoff
├── sdr_sources.py        # Implemented source abstraction and two sources
└── kiwiclient/           # Existing upstream checkout; leave unchanged
```

The user initially considered UTC-aligned recordings, but subsequently chose
signal-based calibration relative to stream start. Do not revert to UTC-based
windowing. Only FT8 is in scope. Do not use IQ or resample audio.

## Implemented: sdr_sources.py

Uses NumPy and standard-library modules. Kiwi imports are lazy, so WAV replay
does not require loading KiwiClient. Public classes:

```python
class EndReason(Enum):
    EOF = ...
    STOPPED = ...
    ERROR = ...

class AudioHandler(ABC):
    def on_start(self, sample_rate: float) -> None: ...
    def on_data(self, samples: np.ndarray) -> None: ...
    def on_end(self, reason: EndReason,
               error: Exception | None = None) -> None: ...

class AudioSource(ABC):
    @property
    def sample_rate(self) -> float | None: ...
    def run(self, handler: AudioHandler) -> None: ...
    def stop(self) -> None: ...
```

Design choice: subclass AudioHandler for the processing pipeline, not each source.
Both sources deliver to the same handler. run() is synchronous; the application
can invoke it in its producer thread. Callbacks execute serially on that thread.

Contract:

- on_start announces native samples/second before the first data callback.
- sample_rate is None until startup and resets at each run.
- on_data supplies a 1-D mono NumPy int16 array. Delivered arrays remain valid
  after callbacks and later reads; they are not overwritten by the source.
- on_end is called exactly once per accepted run invocation, after cleanup,
  including startup failure. No callbacks follow it in that run.
- EOF means normal WAV exhaustion; STOPPED means stop requested; ERROR carries
  an exception for source failure (including server/connection malfunction).
- stop is thread-safe, repeatable, and harmless when inactive. Established socket
  reads and paced WAV waits are interrupted. Connection setup may take up to
  socket_timeout; a running callback must return before termination completes.
- Source exceptions are reported through on_end rather than raised by run.
  Handler exceptions are reported and then re-raised. Exceptions from on_end
  itself propagate. Concurrent runs on one instance are rejected.
- A source can be run again; a new run is a new stream/calibration session.

### KiwiServerSource

```python
source = KiwiServerSource(
    'web888.servehttp.com', 8074,
    frequency_khz=14075.0,
    low_cut_hz=100,
    high_cut_hz=3000,
    password='',
    user='sdr_sources',
    socket_timeout=10.0,
    # kiwiclient_path='/alternative/checkout',
)
source.run(handler)
```

Wraps a KiwiSDRStream subclass, requests USB with compression disabled and AGC
on, and copies decoded PCM blocks to the handler. Sample rate is announced by
the server; never assume exactly 12000 Hz. Sequence gaps and rate changes end
with ERROR. There is no automatic reconnect yet. No constructor sample-rate
selection is exposed: the current library does not expose arbitrary server-side
rate selection. Add such a parameter only if actual server support is established.

KiwiClient is designed to run from a checkout, not pip install: no setup.py or
pyproject.toml. Its README says to clone/download, install NumPy, and run scripts.
sdr_sources.py adds the sibling kiwiclient checkout (or kiwiclient_path) to
sys.path when connecting, to import kiwi and bundled mod_pywebsocket.

### WavFileSource

```python
source = WavFileSource('recording.wav', block_samples=512, realtime=False)
source.run(handler)
```

Accepts mono 16-bit PCM WAV. Reads the native rate from the header. No resampling.
512 samples is the usual uncompressed Kiwi audio block size. Last block can be
shorter. Unsupported WAV formats are rejected via on_end(ERROR, error).
realtime=False replays as fast as callbacks allow; True uses monotonic,
sample-count-based pacing with interruptible waits.

### Validation already performed

Temporary, ad hoc tests passed for:

- Exact WAV sample preservation and block lengths 512, 512, 76 for 1100 samples.
- Native-rate on_start and EOF lifecycle.
- stop from a callback, repeated inactive stop, and stop during paced replay.
- Missing WAV startup error and propagation of handler exceptions.
- Kiwi adapter with simulated WebSocket MSG sample_rate and binary SND frames.

Live server reception has NOT been tested. No persistent test suite was created.
Existing untracked files inside kiwiclient (decoded.txt, jt9_wisdom.dat, timer.out)
pre-date this implementation; do not remove or claim them as new work.

## Agreed design: two-phase FT8 processing (NOT implemented yet)

Keep acquisition independent of expensive calibration, decoding and uploads.
A single producer / single consumer queue connects the receiver/processor to a
worker. The receiver callback accumulates or chunks after every incoming block.
The consumer runs calibration in phase 1 and decoding in phase 2. A small result
queue or synchronized feedback state communicates calibration completion back
to the producer. Keep on_data lightweight; do not run decoders there.

### Calibrator interface

The user explicitly chose a simple black-box interface:

```python
def calibrate(samples: np.ndarray, sample_rate: float) -> float | None:
    ...
```

Input can be an array or a stable view. None means unsuccessful: keep accumulating
and retry. A float is the FIRST chunking boundary in seconds relative to the
beginning of the supplied array. The calibrator owns the timing inference; do
not require the source/pipeline to understand decoder DT or UTC.

If the supplied array starts at stream sample index S and the returned offset is
b seconds, target boundary n is:

```python
S + round((b + 15.0 * n) * sample_rate)  # n = 0, 1, 2, ...
```

Retain the anchor and derive every target independently. Never repeatedly add
a rounded block count, which would accumulate error.

### Phase 1: accumulation and calibration

1. Start a new stream-relative sample counter at zero.
2. Accumulate received blocks and submit an initial L-second candidate to the
   calibrator when enough data exists.
3. On None, continue accumulating and retry with an appropriate candidate.
4. On a returned offset, establish the first stream-relative boundary and move
   to phase 2. Convert using the actual sample rate and candidate start index.

Still to decide: L, retry advance/cadence, and whether later calls receive a
larger accumulated prefix or a fixed-length shifted candidate. A fixed-length
candidate was suggested earlier, but the user has not finalized that policy.
Prefer at most one outstanding calibration job to avoid stale-job backlogs.
Array views must remain valid and unchanged while the worker uses them.

### Phase 2: continuous detection/decoding

Chunk at the calibrated boundary and every 15 seconds thereafter. The user
explicitly accepts intact incoming blocks: no need to split a block at a target
boundary. Suggested policy is the nearest block boundary, keeping actual sample
positions. At 12 kHz with 512-sample blocks, nearest-boundary error is about
21 ms maximum. Actual block size/rate may differ, so do not hardcode this error.

A flag (proposed name: decode_backlog / --decode-backlog) controls transition:

- Enabled: chunk and decode retained phase-1 history aligned to the acquired
  boundary, then continue with new data.
- Disabled: skip accumulated history; begin with new complete aligned windows
  after the transition.

The exact transition cutoff should be explicit in implementation. Avoid duplicate
reporting if calibration itself generates messages that are also decoded during
history replay. The interval is fixed at 15 seconds for FT8. Earlier discussion
included capturing approximately 13 seconds per interval; 15-second full windows
were recommended, but payload duration should be settled during pipeline work.

Bound queues/history or provide disk spooling. Define overflow behavior rather
than block live reception indefinitely or grow memory without limit. A normal
empty decode does not mean timing is lost. Disconnects, missing audio or rate
changes require a new source session and recalibration. Define EOF/stop behavior
for queued complete windows and discard or explicitly mark incomplete tails.

## Decoder and upload work still outstanding

The user has used:

```bash
./kiwirecorder.py -s web888.servehttp.com -p 8074 -f 14075.0 -m usb
jt9 -8 20260918T124800Z_14075000_usb.wav
```

/usr/bin/jt9 exists. Its help confirms -8 for FT8, -p for period, and writable
path options. Proposed decoder invocation is jt9 -8 -p 15 <absolute WAV path>.
Use a separate working directory because jt9 creates support/output files.
Run it in a subprocess from the consumer worker, with timeout and captured output.
Check installed decoder behavior before relying on filename-derived timestamps
or DT for the calibrator. Success alone does not uniquely determine a boundary;
calibrator internals are a separate task behind the agreed simple interface.

IMPORTANT: The user rejected resampling for now. Do not silently standardize
source/calibrator samples at 12 kHz. Verify what jt9 accepts at the actual native
rate; decoder compatibility is still unresolved if the rate differs from its
expected input. WAV header rate is integer while the source may announce a float;
handling this at the decoder boundary also remains to be determined.

Sending decoded messages to the user's server is an eventual goal; endpoint,
authentication, payload schema and retry policy have not been specified or
implemented. Earlier suggestions included a durable outbox and idempotent batch
IDs, but those are proposals rather than finalized requirements.

## Protocol facts from this checkout

- WebSocket over TCP, sound endpoint /<session-id>/SND.
- Ordinary USB SND messages contain 3-byte tag, 1-byte flags, 4-byte little-endian
  sequence, 2-byte big-endian S-meter, then sample payload.
- Normal uncompressed mono payload is signed 16-bit big-endian PCM on the wire;
  KiwiSDRStream converts it to native NumPy int16.
- Typical uncompressed blocks are 512 samples; compressed audio commonly expands
  to 2048 samples. Code must use actual len(samples).
- Sample rate is announced via MSG sample_rate. Usually 12 kHz on KiwiSDR, but
  the configured host is Web-888 and must not be assumed to use that rate.
- USB blocks have no absolute capture timestamps. Sample count provides relative
  timing. Our chosen signal-based calibration avoids dependence on UTC arrivals.
- WAV is a client-side container; the server streams samples, not WAV files.

Useful upstream references:

- kiwiclient/kiwi/client.py: KiwiSDRStream, audio parsing and callbacks.
- kiwiclient/kiwirecorder.py: existing recorder, setup and WAV writing examples.
- kiwiclient/kiwi/worker.py: existing worker; the new source does not use it.

## Suggested next steps

1. Review sdr_sources.py and test a short live connection and cancellation.
2. Implement/test the FT8 processor with synthetic calibration callbacks and WAV
   replay first; settle candidate retry policy, L and history limits.
3. Implement the real calibrator and jt9 wrapper behind their interfaces.
4. Test backlog modes, block-boundary rounding, EOF and discontinuity recovery.
5. Add result delivery once the user's server contract is available.
