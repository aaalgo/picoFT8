#!/usr/bin/env python3
"""Receive or replay 12 kHz PCM and print FT8 detections as JSON lines."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import queue
import sys
import threading
import time
import wave
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from typing import BinaryIO, Callable

import numpy as np

import jt9
from sdr_sources import AudioHandler, EndReason, KiwiServerSource, WavFileSource

SAMPLE_RATE = 12000
PERIOD = 15
PERIOD_SAMPLES = SAMPLE_RATE * PERIOD
_STOP = object()


@dataclass(frozen=True)
class DecoderConfig:
    calibration_initial_seconds: float = 20.0
    calibration_step_seconds: float = 5.0
    calibration_max_seconds: float = 300.0
    queue_size: int = 32

    def __post_init__(self):
        for value in (self.calibration_initial_seconds,
                      self.calibration_step_seconds, self.calibration_max_seconds):
            if not math.isfinite(value) or value <= 0:
                raise ValueError('Calibration durations must be positive and finite')
        if self.calibration_initial_seconds > self.calibration_max_seconds:
            raise ValueError('Calibration limit must be at least the initial duration')
        if self.queue_size < 1:
            raise ValueError('Queue size must be positive')


@dataclass(frozen=True)
class AudioChunk:
    index: int
    start_sample: int
    start_utc_ns: int
    samples: np.ndarray
    sample_rate: int = SAMPLE_RATE


@dataclass(frozen=True)
class Detection:
    chunk_index: int
    chunk_start_sample: int
    chunk_start_utc_ns: int
    recording_utc_ns: int
    calibrated_utc_ns: int
    delay_seconds: float
    timestamp_source: str
    decode: jt9.DecodeRecord


def detection_for(chunk: AudioChunk, record: jt9.DecodeRecord,
                  timestamp_source: str) -> Detection:
    # DT excludes FT8's nominal 0.5-second signal-start delay.
    slot_ns = chunk.start_utc_ns + round(
        (record.window_start_seconds + record.dt_seconds) * 1_000_000_000)
    period_ns = PERIOD * 1_000_000_000
    calibrated_ns = ((slot_ns + period_ns // 2) // period_ns) * period_ns
    return Detection(chunk.index, chunk.start_sample, chunk.start_utc_ns,
                     slot_ns + 500_000_000, calibrated_ns,
                     (slot_ns - calibrated_ns) / 1_000_000_000,
                     timestamp_source, record)


def detection_message(detection: Detection, site_id: int | None = None) -> dict:
    message = asdict(detection)
    if site_id is not None:
        message['site_id'] = site_id
    return message


def print_detection(detection: Detection, site_id: int | None = None) -> None:
    print(json.dumps(detection_message(detection, site_id)), flush=True)


class MasterPoster:
    def __init__(self, master: str | None, site_id: int | None = None):
        self.url = f'http://{master}/api/rx/' if master else None
        self.site_id = site_id
        self.failed = False

    def __call__(self, detections: list[Detection]) -> None:
        if not self.url or self.failed or not detections:
            return
        records = [detection_message(detection, self.site_id) for detection in detections]
        try:
            request = Request(self.url, data=json.dumps(records).encode('utf-8'),
                              headers={'Content-Type': 'application/json'}, method='POST')
            with urlopen(request, timeout=10) as response:
                if not 200 <= response.status < 300:
                    raise OSError(f'HTTP {response.status}')
        except Exception as exc:
            self.failed = True
            print(f'Failed to post to master; not trying in future: {exc}',
                  file=sys.stderr, flush=True)


def load_recording_metadata(path: Path) -> tuple[dict, bytes] | None:
    """Load a sidecar without changing its formatting or unknown fields."""
    try:
        raw = path.with_suffix('.json').read_bytes()
    except FileNotFoundError:
        return None
    metadata = json.loads(raw)
    if not isinstance(metadata, dict) or type(metadata.get('start_utc_ns')) is not int:
        raise ValueError(f'{path.with_suffix(".json")}: start_utc_ns must be integer Unix nanoseconds')
    if metadata.get('sample_rate', SAMPLE_RATE) != SAMPLE_RATE:
        raise ValueError('Recording metadata must specify 12000 Hz')
    return metadata, raw


def same_path(left: Path, right: Path) -> bool:
    return (left.resolve() == right.resolve()
            or (left.exists() and right.exists() and left.samefile(right)))


class FT8Decoder(AudioHandler):
    """One producer owns the tail; one worker owns decoding and output.

    Calibration is synchronous in on_data. result.mean is used literally as
    the first chunk offset, with no nominal-start adjustment. Each instance
    handles one source session. Complete queued chunks drain on source end.
    """

    def __init__(self, config: DecoderConfig | None = None, *,
                 start_utc_ns: int | None = None, live: bool = False,
                 record_path: str | Path | None = None,
                 record_metadata: bytes | None = None,
                 timestamp_source: str | None = None,
                 stop_source: Callable[[], None] | None = None,
                 calibrate: Callable = jt9.calibrate,
                 decode: Callable = jt9.decode,
                 emit: Callable = print_detection,
                 on_batch: Callable | None = None,
                 clock: Callable[[], int] = time.time_ns):
        self.config = config or DecoderConfig()
        self.start_utc_ns = start_utc_ns
        self.first_callback_utc_ns: int | None = None
        self.timestamp_source = timestamp_source or ('callback_arrival' if start_utc_ns is None else 'explicit')
        self.live = live
        self.record_path = Path(record_path) if record_path is not None else None
        self._record_metadata = record_metadata
        if self.record_path is not None and same_path(self.record_path, self.record_path.with_suffix('.json')):
            raise ValueError('Recording WAV and JSON paths must be different')
        self._recording: wave.Wave_write | None = None
        self._recording_file: BinaryIO | None = None
        self._recording_pending_samples = 0
        self.stop_source = stop_source or (lambda: None)
        self.calibrate, self.decode, self.emit, self.clock = calibrate, decode, emit, clock
        self.on_batch = on_batch
        self.stage = 'CALIBRATING'
        self.calibration_result = None
        self.next_calibration_seconds = self.config.calibration_initial_seconds
        self.total_samples_received = 0
        self.buffer_start_sample = 0
        self.next_chunk_index = 0
        self._storage = np.empty(0, dtype=np.int16)
        self._head = self._end = 0
        self._queue = queue.Queue(maxsize=self.config.queue_size)
        self._worker: threading.Thread | None = None
        self.worker_error: Exception | None = None
        self.error: Exception | None = None
        self.end_reason: EndReason | None = None

    @property
    def buffered_samples(self) -> int:
        return self._end - self._head

    def on_start(self, sample_rate: float) -> None:
        # Servers can advertise nominal 12 kHz with fractional reporting error
        # (e.g. Web-888: 11999.999984). PCM remains unchanged; jt9 uses 12000.
        assert math.isclose(sample_rate, SAMPLE_RATE, rel_tol=0, abs_tol=0.001), (
            f'FT8 requires 12000 Hz (within 0.001 Hz); server/source reported '
            f'{sample_rate!r} Hz; no resampling')
        if self._worker is not None or self.stage == 'ENDED':
            raise RuntimeError('Use a new FT8Decoder for each source session')
        if self.record_path is not None:
            self._recording_file = self.record_path.open('wb')
            self._recording = wave.open(self._recording_file, 'wb')
            self._recording.setparams((1, 2, SAMPLE_RATE, 0, 'NONE', 'not compressed'))
            self._write_metadata()
        self._worker = threading.Thread(target=self._consume, name='ft8-decoder')
        self._worker.start()

    def _write_metadata(self) -> None:
        if self.record_path is None:
            return
        raw = self._record_metadata
        if raw is None:
            raw = (json.dumps(dict(format_version=1, sample_rate=SAMPLE_RATE,
                                   start_utc_ns=self.start_utc_ns,
                                   timestamp_source=self.timestamp_source), indent=2) + '\n').encode('utf-8')
        self.record_path.with_suffix('.json').write_bytes(raw)

    def _append(self, samples: np.ndarray) -> None:
        needed = self.buffered_samples + samples.size
        if self._end + samples.size > self._storage.size:
            capacity = self._storage.size
            if needed > capacity:
                capacity = max(needed, capacity * 2, PERIOD_SAMPLES)
            storage = np.empty(capacity, dtype=np.int16)
            storage[:self.buffered_samples] = self._storage[self._head:self._end]
            self._end = self.buffered_samples
            self._head = 0
            self._storage = storage
        self._storage[self._end:self._end + samples.size] = samples
        self._end += samples.size
        self.total_samples_received += samples.size

    def _discard(self, count: int) -> None:
        self._head += count
        self.buffer_start_sample += count
        if self._head == self._end:
            self._head = self._end = 0

    def _check_worker(self) -> None:
        if self.worker_error is not None:
            raise RuntimeError('FT8 decoder worker failed') from self.worker_error

    def _put(self, item, *, draining: bool = False) -> None:
        while True:
            self._check_worker()
            try:
                if self.live and not draining:
                    self._queue.put_nowait(item)
                else:
                    self._queue.put(item, timeout=0.1)
                return
            except queue.Full:
                if self.live and not draining:
                    raise RuntimeError('FT8 decode queue is full; stopping live reception')

    def _dispatch(self) -> None:
        while self.buffered_samples >= PERIOD_SAMPLES:
            samples = self._storage[self._head:self._head + PERIOD_SAMPLES].copy()
            samples.flags.writeable = False
            start_ns = self.start_utc_ns + (
                self.buffer_start_sample * 1_000_000_000 + SAMPLE_RATE // 2) // SAMPLE_RATE
            self._put(AudioChunk(self.next_chunk_index, self.buffer_start_sample,
                                 start_ns, samples))
            self.next_chunk_index += 1
            self._discard(PERIOD_SAMPLES)

    def on_data(self, samples: np.ndarray) -> None:
        if self._worker is None or self.stage == 'ENDED':
            raise RuntimeError('Audio received outside an active source session')
        assert samples.ndim == 1 and samples.dtype == np.int16, 'Expected mono int16 PCM'
        if not samples.size:
            return
        if self.first_callback_utc_ns is None:
            self.first_callback_utc_ns = self.clock()
            if self.start_utc_ns is None:
                self.start_utc_ns = self.first_callback_utc_ns
                self._write_metadata()
        if self._recording is not None:
            self._recording.writeframesraw(samples.astype('<i2', copy=False).tobytes())
            self._recording_pending_samples += samples.size
            # Checkpoint once per FT8 period, including during calibration.
            # An empty writeframes updates the header without adding audio.
            if self._recording_pending_samples >= PERIOD_SAMPLES:
                self._recording.writeframes(b'')
                self._recording_file.flush()
                self._recording_pending_samples = 0
        self._check_worker()
        self._append(samples)
        if self.stage == 'DECODING':
            self._dispatch()
            return
        duration = self.buffered_samples / SAMPLE_RATE
        if duration >= self.next_calibration_seconds:
            try:
                result = self.calibrate(self._storage[self._head:self._end], SAMPLE_RATE)
            except jt9.InsufficientAudioError:
                result = None
            if result is not None and result.mean is not None:
                offset = result.mean
                if not math.isfinite(offset) or not 0 <= offset <= duration:
                    raise ValueError(f'Invalid calibration chunk offset: {offset}')
                self.calibration_result = result
                self._discard(round(offset * SAMPLE_RATE))
                print(f'Calibrated: first chunk offset={offset:.6f}s, '
                      f'sigma={result.sigma}', file=sys.stderr)
                self._dispatch()
                self.stage = 'DECODING'
                return
            self.next_calibration_seconds += self.config.calibration_step_seconds
        if duration >= self.config.calibration_max_seconds:
            raise RuntimeError('Calibration duration limit reached without a decoded signal')

    def _consume(self) -> None:
        try:
            while True:
                chunk = self._queue.get()
                if chunk is _STOP:
                    return
                assert chunk.sample_rate == SAMPLE_RATE
                detections = [detection_for(chunk, record, self.timestamp_source)
                              for record in self.decode(chunk.samples, chunk.sample_rate)]
                for detection in detections:
                    self.emit(detection)
                if detections and self.on_batch is not None:
                    self.on_batch(detections)
        except Exception as exc:
            self.worker_error = exc
            self.stop_source()

    def on_end(self, reason: EndReason, error: Exception | None = None) -> None:
        self.end_reason, self.error = reason, error
        if self._recording is not None:
            recording, self._recording = self._recording, None
            try:
                recording.close()  # Finalize the WAV length even on stop/error.
            except Exception as exc:
                self.error = self.error or exc
        if self._recording_file is not None:
            recording_file, self._recording_file = self._recording_file, None
            try:
                recording_file.close()
            except Exception as exc:
                self.error = self.error or exc
        if self.stage == 'CALIBRATING' and self.error is None and reason == EndReason.EOF:
            self.error = RuntimeError('WAV ended before calibration succeeded')
        tail = self.buffered_samples
        self.stage = 'ENDED'
        if self._worker is not None:
            try:
                self._put(_STOP, draining=True)
            except RuntimeError:
                pass  # The worker failure is retained and reported below.
            self._worker.join()
        self.error = self.worker_error or self.error
        self._storage = np.empty(0, dtype=np.int16)
        self._head = self._end = 0
        if tail:
            print(f'Discarded {tail / SAMPLE_RATE:.6f}s of unprocessed audio', file=sys.stderr)


def parse_utc(value: str) -> int:
    """Accept Unix seconds or a timezone-qualified ISO 8601 timestamp."""
    from decimal import Decimal, InvalidOperation
    try:
        seconds = Decimal(value)
    except InvalidOperation:
        try:
            timestamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if timestamp.tzinfo is None:
                raise ValueError('timezone required')
            delta = timestamp.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
            return ((delta.days * 86400 + delta.seconds) * 1_000_000_000
                    + delta.microseconds * 1000)
        except ValueError as exc:
            raise argparse.ArgumentTypeError('Use Unix seconds or ISO UTC with a timezone') from exc
    if not seconds.is_finite():
        raise argparse.ArgumentTypeError('UTC timestamp must be finite')
    return int(seconds * 1_000_000_000)


def parse_address(value: str) -> str:
    try:
        address = urlsplit(f'//{value}')
        if (not address.hostname or address.port is None or
                not 1 <= address.port <= 65535 or address.username is not None or
                address.password is not None or address.path or address.query or
                address.fragment or any(char.isspace() for char in value)):
            raise ValueError
    except ValueError as exc:
        raise argparse.ArgumentTypeError('Use host:port (port 1-65535)') from exc
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument('-s', '--server', type=parse_address, metavar='HOST:PORT')
    sources.add_argument('-f', '--file', type=Path)
    parser.add_argument('--record', type=Path, metavar='PATH.wav',
                        help='save all received PCM, including calibration audio and final tail')
    parser.add_argument('--master', type=parse_address, default='127.0.0.1:7777',
                        metavar='HOST:PORT', help='HTTP master (default: 127.0.0.1:7777)')
    parser.add_argument('--frequency-khz', type=float, help='USB dial frequency (default: 14075)')
    parser.add_argument('--low-cut-hz', type=int, help='USB low cut (default: 100)')
    parser.add_argument('--high-cut-hz', type=int, help='USB high cut (default: 3000)')
    parser.add_argument('--password')
    parser.add_argument('--user')
    parser.add_argument('--site_id', type=int,
                        help='include this site ID in each detection JSON message')
    parser.add_argument('--socket-timeout', type=float)
    parser.add_argument('--kiwiclient-path', type=Path)
    parser.add_argument('--block-samples', type=int, help='WAV block size (default: 512)')
    parser.add_argument('--realtime', action='store_true')
    parser.add_argument('--start-utc', type=parse_utc, default=None,
                        help='override WAV sample-zero UTC (default: JSON sidecar, otherwise 0)')
    parser.add_argument('--calibration-initial-seconds', type=float, default=20)
    parser.add_argument('--calibration-step-seconds', type=float, default=5)
    parser.add_argument('--calibration-max-seconds', type=float, default=300)
    parser.add_argument('--queue-size', type=int, default=32)
    args = parser.parse_args(argv)
    if args.server and (args.start_utc is not None or args.realtime or args.block_samples is not None):
        parser.error('--start-utc, --realtime and --block-samples are WAV options')
    live_defaults = dict(frequency_khz=14075.0, low_cut_hz=100,
                         high_cut_hz=3000, password='', user='sdr_ft8_client',
                         socket_timeout=10.0, kiwiclient_path=None)
    if args.file and any(getattr(args, name) is not None for name in live_defaults):
        parser.error('Server tuning and connection options cannot be used with --file')
    for name, default in live_defaults.items():
        if getattr(args, name) is None:
            setattr(args, name, default)
    try:
        if args.file is not None and args.record is not None:
            for output in (args.record, args.record.with_suffix('.json')):
                for source_path in (args.file, args.file.with_suffix('.json')):
                    if same_path(output, source_path):
                        raise ValueError('--record must not overwrite the input WAV or JSON')
        config = DecoderConfig(args.calibration_initial_seconds, args.calibration_step_seconds,
                               args.calibration_max_seconds, args.queue_size)
        metadata_raw = None
        timestamp_source = None
        if args.server:
            address = urlsplit(f'//{args.server}')
            source = KiwiServerSource(address.hostname, address.port, frequency_khz=args.frequency_khz,
                                      low_cut_hz=args.low_cut_hz, high_cut_hz=args.high_cut_hz,
                                      password=args.password, user=args.user,
                                      socket_timeout=args.socket_timeout,
                                      kiwiclient_path=args.kiwiclient_path)
            start_ns = None
        else:
            source = WavFileSource(args.file,
                                   block_samples=512 if args.block_samples is None else args.block_samples,
                                   realtime=args.realtime)
            loaded = load_recording_metadata(args.file)
            start_ns = args.start_utc if args.start_utc is not None else 0
            if loaded is not None:
                metadata, metadata_raw = loaded
                if args.start_utc is None:
                    start_ns = metadata['start_utc_ns']
                if start_ns == metadata['start_utc_ns']:
                    timestamp_source = metadata.get('timestamp_source', 'explicit')
                else:
                    metadata.update(start_utc_ns=start_ns, timestamp_source='explicit')
                    metadata_raw = (json.dumps(metadata, indent=2) + '\n').encode('utf-8')
        handler = FT8Decoder(config, start_utc_ns=start_ns, live=bool(args.server),
                             stop_source=source.stop, record_path=args.record,
                             record_metadata=metadata_raw, timestamp_source=timestamp_source,
                             emit=lambda detection: print_detection(detection, site_id=args.site_id),
                             on_batch=MasterPoster(args.master, args.site_id))
        try:
            source.run(handler)
        except KeyboardInterrupt:
            source.stop()
            return 130
        if handler.error is not None:
            raise handler.error
    except Exception as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
