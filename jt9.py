#!/usr/bin/env python3
"""Decode FT8 audio windows or complete recordings using jt9.

Requires numpy, scipy, and the jt9 executable on PATH. Long inputs are not
split into slots by decode(); use decode_all() to align and decode each period.
"""

import argparse
from dataclasses import dataclass, replace
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import NamedTuple

import numpy as np
from scipy.io import wavfile

from power import find_1st_slot


@dataclass(frozen=True)
class DecodeRecord:
    """One decoded transmission; the message is preserved without interpretation."""

    utc: str
    """Decoder HHMMSS label, always '000000' here; not a known recording time."""
    snr_db: int
    """Received SNR in dB, referenced to a 2500 Hz noise bandwidth."""
    dt_seconds: float
    """Timing relative to the nominal 0.5-second start, rounded to 0.1 s."""
    frequency_hz: int
    """Audio frequency, not the receiver's RF frequency."""
    mode: str
    """Decoder mode marker: '~' for FT8."""
    message: str
    """Entire payload, including unresolved hashes and compound messages."""
    window_start_seconds: float = 0.0
    """Start of the decoded window within the original input audio."""

    @property
    def start_offset_seconds(self) -> float:
        """Estimated start relative to input sample zero; may be negative."""
        return self.window_start_seconds + round(self.dt_seconds + 0.5, 1)


class CalibrationResult(NamedTuple):
    """Gaussian parameters in seconds and detections grouped by period.

    mean and sigma are None when no signals were decoded. Empty periods are
    retained in detections so their indices still identify the correct slots.
    """

    mean: float | None
    sigma: float | None
    detections: list[list[DecodeRecord]]


class InsufficientAudioError(ValueError):
    """No complete period is available after the estimated first boundary."""


_DECODE_LINE = re.compile(
    r"^\s*(\d{6})\s+([+-]?\d+)\s+([+-]?\d+\.\d+)\s+"
    r"(\d+)\s+(~)\s+(.+?)\s*$"
)


def _validate_audio(samples: np.ndarray, sample_rate: int) -> None:
    if (isinstance(sample_rate, (bool, np.bool_))
            or not isinstance(sample_rate, (int, np.integer))
            or sample_rate != 12000):
        raise ValueError("sample_rate must be 12000 Hz (integer)")
    if not isinstance(samples, np.ndarray) or samples.ndim != 1 or not samples.size:
        raise ValueError("samples must be a nonempty, one-dimensional numpy array")
    if samples.dtype.kind not in "iuf":
        raise ValueError("samples must contain real floating-point or integer PCM audio")
    if not np.isfinite(samples).all():
        raise ValueError("samples must be finite")


def decode_all(
    samples: np.ndarray, sample_rate: int, period: float = 15.0,
    first_only: bool = False, merged: bool = True,
) -> list[DecodeRecord] | list[list[DecodeRecord]]:
    """Find the first power dip and decode successive complete periods.

    period is in seconds and controls both the dip-search periodicity and the
    extracted duration. The power integration window and step are period / 50.
    Audio requirements and return records are the same as for decode().
    start_offset_seconds includes the window's offset in the original input;
    dt_seconds preserves jt9's original window-relative DT field.
    first_only stops after the first period. merged returns a flat list in
    period order; otherwise each period has its own list, including empty ones.
    Repeated messages are preserved. An incomplete trailing period is skipped.
    Raises ValueError if a full window is unavailable after the first dip.
    Changing period does not change jt9's FT8 timing or first-slot-only limit.
    """
    _validate_audio(samples, sample_rate)
    if (isinstance(period, (bool, np.bool_))
            or not isinstance(period, (int, float, np.integer, np.floating))
            or not np.isfinite(period) or period <= 0):
        raise ValueError("period must be a finite positive number of seconds")
    length = round(period * sample_rate)
    if round(period / 50 * sample_rate) < 1:
        raise ValueError("period / 50 must span at least one sample")
    offset = find_1st_slot(
        samples, sample_rate, period=period, window=period / 50, step=period / 50,
    )
    start = round(offset * sample_rate)
    if start < 0 or start + length > samples.size:
        raise InsufficientAudioError("Not enough audio for a complete period after the first dip")
    outcomes: list[list[DecodeRecord]] = []
    for window_start in range(start, samples.size - length + 1, length):
        records = decode(samples[window_start:window_start + length], sample_rate)
        outcomes.append([
            replace(record, window_start_seconds=window_start / sample_rate)
            for record in records
        ])
        if first_only:
            break
    if merged:
        return [record for outcome in outcomes for record in outcome]
    return outcomes


def decode(samples: np.ndarray, sample_rate: int) -> list[DecodeRecord]:
    """Write mono audio to a temporary WAV and return jt9 -8's decodes.

    Signed integers use their dtype's full PCM range; unsigned integers are
    centered at their dtype's midpoint. Floats use [-1, 1] full scale and are
    clipped when converted to int16. Non-finite and complex samples are rejected.
    sample_rate must be the integer 12000 Hz. Short inputs are zero-padded to
    15 seconds; longer inputs retain jt9's first-slot-only behavior.

    The nominal transmission start should be near 0.5 seconds into the input.
    Returns [] for successful decoding with no messages. Invalid input raises
    ValueError; missing jt9, decoder failure, and a 60-second timeout propagate
    as FileNotFoundError, CalledProcessError, and TimeoutExpired respectively.
    Unexpected stdout raises RuntimeError instead of silently losing records.
    All temporary files, including jt9's side files, are cleaned up on exit.
    """
    _validate_audio(samples, sample_rate)
    audio = samples.astype(np.float64)
    if samples.dtype.kind in "iu":
        midpoint = float(2 ** (samples.dtype.itemsize * 8 - 1))
        if samples.dtype.kind == "u":
            audio -= midpoint
        audio /= midpoint
    pcm = np.clip(np.rint(np.clip(audio, -1, 1) * 32768), -32768, 32767).astype(np.int16)
    if pcm.size < 180000:
        pcm = np.pad(pcm, (0, 180000 - pcm.size))

    with TemporaryDirectory(prefix="jt9-") as directory:
        path = Path(directory) / "audio_000000.wav"
        wavfile.write(path, 12000, pcm)
        result = subprocess.run(
            ["jt9", "-8", str(path)], cwd=directory,
            capture_output=True, text=True, check=True, timeout=60,
        )

    records = []
    for line in result.stdout.splitlines():
        if not line.strip() or line.lstrip().startswith("<DecodeFinished>"):
            continue
        match = _DECODE_LINE.fullmatch(line)
        if match is None:
            raise RuntimeError(f"Unexpected jt9 output: {line!r}")
        utc, snr, dt, frequency, mode, message = match.groups()
        records.append(DecodeRecord(utc, int(snr), float(dt), int(frequency), mode, message))
    return records


def calibrate(samples: np.ndarray, sample_rate: int) -> CalibrationResult:
    """Decode all 15-second periods and fit their aligned signal start times.

    Each detection contributes start_offset_seconds - period_index * 15.
    Return the maximum-likelihood Gaussian mean and sigma (population standard
    deviation), plus all detections. Both fit parameters are None if no signals
    were decoded; a single detection has sigma zero. Requires 12000 Hz audio.
    """
    detections = decode_all(samples, sample_rate, period=15.0, merged=False)
    aligned = np.array([
        record.start_offset_seconds - i * 15.0
        for i, records in enumerate(detections) for record in records
    ])
    if not aligned.size:
        return CalibrationResult(None, None, detections)
    return CalibrationResult(
        float(aligned.mean()), float(aligned.std(ddof=0)), detections,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Decode FT8 periods and fit a Gaussian to aligned signal start times.",
    )
    parser.add_argument("-i", required=True, type=Path, metavar="PATH.wav",
                        help="mono 12000 Hz WAV file (no resampling)")
    args = parser.parse_args()
    period = 15.0
    try:
        sample_rate, samples = wavfile.read(args.i)
        result = calibrate(samples, sample_rate)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    mean, sigma, outcomes = result
    if mean is None:
        print("Gaussian fit unavailable: no decoded signals.")
        print("No decodings.")
        return 0

    count = sum(len(records) for records in outcomes)
    print(f"Gaussian fit: mean={mean:.6f} s, sigma={sigma:.6f} s, n={count}")
    print("Aligned start = recording start offset - window index * 15 s.")
    print("Delta = detected start - (mean + window index * 15 s).")
    print(f"{'UTC':6} {'SNR':>4} {'DT':>5} {'Hz':>5} M {'Message':37} "
          f"{'Window':>6} {'Start(s)':>10} {'Delta(s)':>10}")
    for i, records in enumerate(outcomes):
        for record in records:
            delta = record.start_offset_seconds - (mean + i * period)
            print(f"{record.utc} {record.snr_db:4d} {record.dt_seconds:5.1f} "
                  f"{record.frequency_hz:5d} {record.mode} {record.message:37} "
                  f"{i:6d} {record.start_offset_seconds:10.3f} {delta:+10.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
