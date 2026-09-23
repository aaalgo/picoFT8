"""Callback-based native-rate audio sources for FT8 and other consumers.

Run ``source.run(handler)`` in the application's producer thread. Callbacks are
serial and run on that thread. Arrays are owned by the recipient and remain
valid indefinitely. No resampling or decoding is performed here.

KiwiServerSource loads the sibling ``kiwiclient`` checkout lazily. WAV replay
only needs NumPy. Source failures are delivered through on_end(ERROR, error);
handler exceptions are also reported there, then re-raised to the caller.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum, auto
import math
from pathlib import Path
import socket
import sys
import threading
import time
from types import SimpleNamespace
import wave

import numpy as np

__all__ = ['EndReason', 'AudioHandler', 'AudioSource', 'KiwiServerSource',
           'WavFileSource']


class EndReason(Enum):
    EOF = auto()
    STOPPED = auto()
    ERROR = auto()


class AudioHandler(ABC):
    @abstractmethod
    def on_start(self, sample_rate: float) -> None:
        """Called once after negotiation, before data; rate is samples/second."""

    @abstractmethod
    def on_data(self, samples: np.ndarray) -> None:
        """Receive a contiguous 1-D mono int16 block; keep this callback short."""

    @abstractmethod
    def on_end(self, reason: EndReason, error: Exception | None = None) -> None:
        """Called exactly once per run, after cleanup, even if startup fails."""


class AudioSource(ABC):
    def __init__(self) -> None:
        self._sample_rate: float | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._running = False
        self._handler: AudioHandler | None = None
        self._callback_error: Exception | None = None

    @property
    def sample_rate(self) -> float | None:
        """Native rate; None until startup completes. Reset at each run."""
        return self._sample_rate

    def _call(self, name: str, *args) -> None:
        try:
            getattr(self._handler, name)(*args)
        except Exception as exc:
            self._callback_error = exc
            raise

    def _start(self, rate: float) -> None:
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError(f'Invalid sample rate: {rate}')
        self._sample_rate = float(rate)
        if not self._stop_event.is_set():
            self._call('on_start', self._sample_rate)

    def _data(self, samples: np.ndarray) -> None:
        if not self._stop_event.is_set():
            self._call('on_data', samples)

    def run(self, handler: AudioHandler) -> None:
        """Run synchronously until EOF, failure, or stop; may be run again.

        Concurrent runs are rejected. Source errors go to on_end, not the
        caller. Exceptions from handlers propagate after cleanup and on_end.
        """
        with self._lock:
            if self._running:
                raise RuntimeError('Source is already running')
            self._running = True
            self._stop_event.clear()
            self._sample_rate = None
            self._handler = handler
            self._callback_error = None
        reason, error = EndReason.EOF, None
        try:
            try:
                self._receive()
                if self._stop_event.is_set():
                    reason = EndReason.STOPPED
            except Exception as exc:
                if self._stop_event.is_set() and self._callback_error is None:
                    reason = EndReason.STOPPED
                else:
                    reason, error = EndReason.ERROR, exc
            finally:
                try:
                    self._cleanup()
                except Exception as exc:
                    if error is None:
                        reason, error = EndReason.ERROR, exc
            handler.on_end(reason, error)
            if self._callback_error is not None:
                raise self._callback_error
        finally:
            with self._lock:
                self._running = False
                self._handler = None

    def stop(self) -> None:
        """Thread-safe stop request; inactive calls are harmless.

        Interrupts established socket reads and paced replay. Connection setup
        can take up to socket_timeout; an executing handler must return first.
        """
        with self._lock:
            if self._running:
                self._stop_event.set()
                self._interrupt()

    @abstractmethod
    def _receive(self) -> None:
        ...

    def _interrupt(self) -> None:
        pass

    def _cleanup(self) -> None:
        pass


class WavFileSource(AudioSource):
    def __init__(self, path: str | Path, block_samples: int = 512, *,
                 realtime: bool = False) -> None:
        """Replay mono 16-bit PCM WAV, using typical Kiwi PCM block size.

        Without realtime, deliver as fast as callbacks allow. With realtime,
        pace blocks by their ending sample position using a monotonic clock.
        """
        super().__init__()
        if not isinstance(block_samples, int) or isinstance(block_samples, bool) or block_samples <= 0:
            raise ValueError('block_samples must be a positive integer')
        self.path = Path(path)
        self.block_samples = block_samples
        self.realtime = realtime

    def _receive(self) -> None:
        with wave.open(str(self.path), 'rb') as wav:
            if (wav.getnchannels() != 1 or wav.getsampwidth() != 2
                    or wav.getcomptype() != 'NONE'):
                raise ValueError('Expected mono, 16-bit PCM WAV')
            self._start(wav.getframerate())
            count = 0
            started = time.monotonic()
            while not self._stop_event.is_set():
                raw = wav.readframes(self.block_samples)
                if not raw:
                    if count != wav.getnframes():
                        raise ValueError('Truncated WAV audio data')
                    return
                samples = np.frombuffer(raw, dtype='<i2').astype(np.int16, copy=True)
                count += len(samples)
                if self.realtime:
                    delay = started + count / self.sample_rate - time.monotonic()
                    if self._stop_event.wait(max(0.0, delay)):
                        return
                self._data(samples)


class KiwiServerSource(AudioSource):
    def __init__(self, host: str, port: int = 8073, *,
                 frequency_khz: float, low_cut_hz: int = 100,
                 high_cut_hz: int = 3000, password: str = '',
                 user: str = 'sdr_sources', socket_timeout: float = 10.0,
                 kiwiclient_path: str | Path | None = None) -> None:
        """Receive native-rate USB PCM; no reconnect or resampling.

        Rate changes and sequence gaps terminate with ERROR so callers can
        restart calibration. kiwiclient_path defaults to the sibling checkout.
        Server-side arbitrary sample-rate selection is not supported.
        """
        super().__init__()
        if not host or not 0 < port < 65536:
            raise ValueError('A host and valid TCP port are required')
        if not math.isfinite(socket_timeout) or socket_timeout <= 0:
            raise ValueError('socket_timeout must be positive and finite')
        if not math.isfinite(frequency_khz) or frequency_khz < 0:
            raise ValueError('frequency_khz must be nonnegative and finite')
        if not 0 <= low_cut_hz < high_cut_hz:
            raise ValueError('Expected 0 <= low_cut_hz < high_cut_hz')
        self.host, self.port = host, port
        self.frequency_khz = frequency_khz
        self.low_cut_hz, self.high_cut_hz = low_cut_hz, high_cut_hz
        self.password, self.user = password, user
        self.socket_timeout = socket_timeout
        self.kiwiclient_path = Path(kiwiclient_path) if kiwiclient_path else Path(__file__).resolve().parent / 'kiwiclient'
        self._client = None

    def _receive(self) -> None:
        # The checkout imports its bundled mod_pywebsocket as a top-level module.
        checkout = str(self.kiwiclient_path.resolve())
        if checkout not in sys.path:
            sys.path.insert(0, checkout)
        from kiwi import KiwiSDRStream

        owner = self

        class Stream(KiwiSDRStream):
            def __init__(self):
                super().__init__()
                self._type = 'SND'
                self._start_time = None
                self._camp_wait_event = None
                self._last_seq = None
                self._options = SimpleNamespace(
                    wideband=False, ws_timestamp=time.time_ns() // 1_000_000,
                    socket_timeout=owner.socket_timeout, nolocal=False,
                    admin=False, password=owner.password, tlimit_password='',
                    idx=0, server_host=owner.host, freq_pbc=False, wf_cal=None,
                    ADC_OV=False, S_meter=-1, sdt=0, netcat=False,
                    tlimit=None, stats=False)

            def _on_sample_rate_change(self):
                if owner.sample_rate is None:
                    owner._start(self._sample_rate)
                elif owner.sample_rate != self._sample_rate:
                    raise RuntimeError('Server sample rate changed; start a new session')

            def _setup_rx_params(self):
                self.set_name(owner.user)
                self.set_mod('usb', owner.low_cut_hz, owner.high_cut_hz,
                             owner.frequency_khz)
                self.set_agc(on=True)
                self._set_snd_comp(False)

            def _process_audio_samples(self, seq, samples, rssi, fmt):
                if owner.sample_rate is None:
                    raise RuntimeError('Audio arrived before sample-rate negotiation')
                if self._last_seq is not None and seq != (self._last_seq + 1) % (1 << 32):
                    raise RuntimeError(f'Audio sequence discontinuity: {self._last_seq} -> {seq}')
                self._last_seq = seq
                owner._data(np.array(samples, dtype=np.int16, copy=True))

        self._client = Stream()
        if self._stop_event.is_set():
            return
        self._client.connect(self.host, self.port)
        if self._stop_event.is_set():
            return
        self._client.open()
        while not self._stop_event.is_set():
            self._client.run()

    def _interrupt(self) -> None:
        client = self._client
        sock = getattr(client, '_socket', None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _cleanup(self) -> None:
        client, self._client = self._client, None
        sock = getattr(client, '_socket', None)
        if sock is not None:
            # This source has no writer thread; avoid Kiwi's global writer queue.
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
