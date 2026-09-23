#!/usr/bin/env python3

import argparse
import numpy as np


DEFAULT_SAMPLE_RATE = 12000
DEFAULT_WINDOW = 2.36
DEFAULT_STEP = 0.5
DEFAULT_PERIOD = 15.0


def compute_power_curve(
    samples: np.ndarray,
    sample_rate: int,
    window: float = DEFAULT_WINDOW,
    step: float = DEFAULT_STEP,
) -> np.ndarray:
    """
    Compute sliding-window mean-square power.

    Parameters
    ----------
    samples:
        1-D mono audio samples.
    sample_rate:
        Sample rate in Hz.
    window:
        Power integration window in seconds.
    step:
        Time between successive power measurements in seconds.

    Returns
    -------
    curve:
        1-D array of mean-square power values.

        curve[i] corresponds to a window starting at:

            i * step

        and centered at:

            i * step + window / 2
    """
    samples = np.asarray(samples)

    if samples.ndim != 1:
        raise ValueError(
            f"Expected mono samples, got shape {samples.shape}"
        )

    if window <= 0:
        raise ValueError("window must be > 0")

    if step <= 0:
        raise ValueError("step must be > 0")

    samples = samples.astype(np.float64, copy=False)

    window_samples = round(window * sample_rate)
    step_samples = round(step * sample_rate)

    if window_samples > len(samples):
        raise ValueError("window is longer than input samples")

    # Instantaneous power.
    energy = samples * samples

    # Prefix sum lets each arbitrary overlapping window
    # be evaluated in O(1).
    csum = np.concatenate(([0.0], np.cumsum(energy)))

    starts = np.arange(
        0,
        len(samples) - window_samples + 1,
        step_samples,
    )

    sums = csum[starts + window_samples] - csum[starts]

    return sums / window_samples


def search_dip(
    curve: np.ndarray,
    period: int = 30,
) -> int:
    """
    Fold a power curve by `period` samples and return the phase
    offset with minimum average power.

    Returns an integer offset in:

        [0, period)

    An incomplete tail is handled by folding both the curve and
    an array of ones, then dividing by the resulting count.
    """
    curve = np.asarray(curve, dtype=np.float64)

    if curve.ndim != 1:
        raise ValueError("curve must be one-dimensional")

    if len(curve) == 0:
        raise ValueError("curve must not be empty")

    if period <= 0:
        raise ValueError("period must be > 0")

    folded = np.zeros(period, dtype=np.float64)
    counts = np.zeros(period, dtype=np.float64)

    ones = np.ones_like(curve)

    for start in range(0, len(curve), period):
        chunk = curve[start:start + period]
        count_chunk = ones[start:start + period]

        folded[:len(chunk)] += chunk
        counts[:len(count_chunk)] += count_chunk

    average = np.full(period, np.inf, dtype=np.float64)

    valid = counts > 0
    average[valid] = folded[valid] / counts[valid]

    return int(np.argmin(average))


def find_1st_slot(
    samples: np.ndarray,
    sample_rate: int,
    window: float = DEFAULT_WINDOW,
    step: float = DEFAULT_STEP,
    period: float = DEFAULT_PERIOD,
) -> float:
    """
    Find the center of the first periodic power dip.

    Parameters
    ----------
    samples:
        Mono audio samples.
    sample_rate:
        Sample rate in Hz.
    window:
        Power integration window in seconds.
    step:
        Power curve step in seconds.
    period:
        Target periodicity in seconds.

    Returns
    -------
    float:
        Center time of the first dip, in seconds.
    """
    curve = compute_power_curve(
        samples,
        sample_rate,
        window=window,
        step=step,
    )

    period_points = round(period / step)

    if period_points <= 0:
        raise ValueError("period must be >= step")

    offset = search_dip(curve, period=period_points)

    # offset refers to the beginning of the integration window.
    # Report the center.
    return offset * step + window / 2


def dip_times(
    first_dip: float,
    duration: float,
    period: float = DEFAULT_PERIOD,
) -> np.ndarray:
    """
    Generate all predicted dip-center times within the recording.
    """
    if period <= 0:
        raise ValueError("period must be > 0")

    if first_dip >= duration:
        return np.empty(0)

    n = int(np.floor((duration - first_dip) / period)) + 1

    return first_dip + np.arange(n) * period


def main():
    from scipy.io import wavfile
    parser = argparse.ArgumentParser(
        description=(
            "Calculate a sliding-window WAV power curve and "
            "find periodic power dips."
        )
    )

    parser.add_argument(
        "-i", "--input",
        required=True,
        metavar="PATH",
        help="input mono 12 kHz WAV file",
    )

    parser.add_argument(
        "--limit",
        type=float,
        metavar="SECONDS",
        help="analyze only the first SECONDS of audio",
    )

    parser.add_argument(
        "-o", "--output",
        default="power.png",
        metavar="PATH",
        help="output plot (default: power.png)",
    )

    parser.add_argument(
        "-w", "--window",
        type=float,
        default=DEFAULT_WINDOW,
        metavar="SECONDS",
        help=(
            "power integration window in seconds "
            f"(default: {DEFAULT_WINDOW})"
        ),
    )

    parser.add_argument(
        "-s", "--step",
        type=float,
        default=DEFAULT_STEP,
        metavar="SECONDS",
        help=(
            "power curve step in seconds "
            f"(default: {DEFAULT_STEP})"
        ),
    )

    parser.add_argument(
        "-p", "--period",
        type=float,
        default=DEFAULT_PERIOD,
        metavar="SECONDS",
        help=(
            "target repeating period in seconds "
            f"(default: {DEFAULT_PERIOD})"
        ),
    )

    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="compare power dips with jt9-calibrated signal offsets",
    )

    parser.add_argument(
        "--lead",
        type=float,
        # FT8 signals start at 0.5 s and end at 15 - 2.36 s in each slot.
        # Plot blank-region centers: half the 2.36 s gap plus the nominal
        # signal offset of 0.5 s gives a lead of 2.36 / 2 + 0.5 = 1.68 s.
        default=1.68,
        metavar="SECONDS",
        help="lead subtracted from calibrated offsets (default: 1.68 seconds)",
    )

    parser.add_argument(
        "--dpi",
        type=int,
        default=150,
        help="output plot DPI (default: 150)",
    )

    args = parser.parse_args()

    if args.limit is not None and (not np.isfinite(args.limit) or args.limit <= 0):
        parser.error("--limit must be a finite positive number of seconds")

    sample_rate, samples = wavfile.read(args.input)

    if sample_rate != DEFAULT_SAMPLE_RATE:
        raise ValueError(
            f"Expected {DEFAULT_SAMPLE_RATE} Hz WAV, "
            f"got {sample_rate} Hz"
        )

    if samples.ndim != 1:
        raise ValueError(
            f"Expected mono WAV, got shape {samples.shape}"
        )

    if args.limit is not None:
        samples = samples[:int(min(args.limit, len(samples) / sample_rate) * sample_rate)]

    curve = compute_power_curve(
        samples,
        sample_rate,
        window=args.window,
        step=args.step,
    )

    first_dip = find_1st_slot(
        samples,
        sample_rate,
        window=args.window,
        step=args.step,
        period=args.period,
    )

    duration = len(samples) / sample_rate

    dips = dip_times(
        first_dip,
        duration,
        period=args.period,
    )

    jt9_times = np.empty(0)
    if args.calibrate:
        import jt9

        result = jt9.calibrate(samples, sample_rate)
        if result.mean is None:
            print("Calibration unavailable: no decoded signals.")
        else:
            jt9_times = dip_times(
                result.mean - args.lead, duration, period=args.period,
            )
            print(f"jt9 offset  : {result.mean:.3f} s")
            print("jt9 times   :", " ".join(f"{t:.3f}" for t in jt9_times))

    times = (
        np.arange(len(curve)) * args.step
        + args.window / 2
    )

    print(f"sample rate : {sample_rate} Hz")
    print(f"duration    : {duration:.3f} s")
    print(f"window      : {args.window:.3f} s")
    print(f"step        : {args.step:.3f} s")
    print(f"period      : {args.period:.3f} s")
    print(f"first dip   : {first_dip:.3f} s")
    print("dips        :", " ".join(f"{t:.3f}" for t in dips))

    # Plotting dependency is deliberately CLI-only.
    import matplotlib.pyplot as plt

    plt.figure(figsize=(12, 5))

    plt.plot(
        times,
        curve,
        linewidth=1,
        label="Power",
    )

    # Predicted dip centers.
    for i, t in enumerate(dips):
        plt.axvline(
            t,
            linestyle="--",
            linewidth=1,
            alpha=0.6,
            label="Dip center" if i == 0 else None,
        )

    for i, t in enumerate(jt9_times):
        plt.axvline(
            t,
            color="tab:orange",
            linestyle=":",
            linewidth=1.5,
            alpha=0.8,
            label="jt9 calibrated offset" if i == 0 else None,
        )

    plt.xlabel("Time (s)")
    plt.ylabel("Mean-square power")
    plt.title(
        f"Power curve "
        f"(window={args.window:g}s, "
        f"step={args.step:g}s, "
        f"period={args.period:g}s)"
    )

    plt.grid(True, alpha=0.3)
    plt.legend()
    plt.tight_layout()

    plt.savefig(
        args.output,
        dpi=args.dpi,
    )

    plt.close()


if __name__ == "__main__":
    main()
