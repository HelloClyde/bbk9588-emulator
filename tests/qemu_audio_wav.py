#!/usr/bin/env python3
"""Finalize and validate PCM captures produced by QEMU's WAV backend."""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import wave
from array import array
from dataclasses import asdict, dataclass
from pathlib import Path

_PCM_WAV_HEADER = struct.Struct("<4sI4s4sIHHIIHH4sI")


@dataclass(frozen=True)
class PcmWavAnalysis:
    path: str
    channels: int
    sample_rate_hz: int
    bits_per_sample: int
    frames: int
    data_bytes: int
    duration_seconds: float
    peak_abs: int
    rms: float
    nonzero_ratio: float
    significant_ratio: float
    clipped_samples: int
    stereo_mismatch_frames: int | None
    first_signal_seconds: float | None
    last_signal_seconds: float | None
    active_span_seconds: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def finalize_qemu_wav_header(path: Path) -> bool:
    """Fill zero RIFF/data lengths left by QEMU's fixed-layout WAV backend.

    Returns True when the header was changed. Non-zero, inconsistent lengths are
    rejected so this compatibility step cannot hide a truncated capture.
    """

    path = Path(path)
    file_size = path.stat().st_size
    if file_size < _PCM_WAV_HEADER.size:
        raise ValueError(f"WAV file is only {file_size} bytes")

    with path.open("r+b") as stream:
        header = stream.read(_PCM_WAV_HEADER.size)
        (
            riff,
            riff_size,
            wave_id,
            fmt_id,
            fmt_size,
            audio_format,
            channels,
            _sample_rate,
            _byte_rate,
            block_align,
            bits_per_sample,
            data_id,
            data_size,
        ) = _PCM_WAV_HEADER.unpack(header)

        if (riff, wave_id, fmt_id, data_id) != (b"RIFF", b"WAVE", b"fmt ", b"data"):
            raise ValueError("expected a fixed 44-byte RIFF/WAVE PCM header")
        if fmt_size != 16 or audio_format != 1:
            raise ValueError("only fixed-layout PCM WAV captures are supported")
        if channels <= 0 or block_align != channels * bits_per_sample // 8:
            raise ValueError("invalid PCM block alignment")

        expected_riff_size = file_size - 8
        expected_data_size = file_size - _PCM_WAV_HEADER.size
        if expected_data_size % block_align:
            raise ValueError("PCM payload ends with a partial frame")
        if riff_size not in (0, expected_riff_size):
            raise ValueError(
                f"RIFF length is {riff_size}, expected {expected_riff_size}"
            )
        if data_size not in (0, expected_data_size):
            raise ValueError(
                f"data length is {data_size}, expected {expected_data_size}"
            )

        changed = riff_size == 0 or data_size == 0
        if changed:
            stream.seek(4)
            stream.write(struct.pack("<I", expected_riff_size))
            stream.seek(40)
            stream.write(struct.pack("<I", expected_data_size))
            stream.flush()
        return changed


def analyze_pcm_wav(
    path: Path,
    *,
    significant_threshold: int = 32,
) -> PcmWavAnalysis:
    path = Path(path)
    if significant_threshold < 1 or significant_threshold > 32768:
        raise ValueError("significant_threshold must be between 1 and 32768")

    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        sample_rate = wav.getframerate()
        sample_width = wav.getsampwidth()
        declared_frames = wav.getnframes()
        if wav.getcomptype() != "NONE" or sample_width != 2:
            raise ValueError("only uncompressed signed 16-bit PCM is supported")
        if channels <= 0 or sample_rate <= 0:
            raise ValueError("invalid WAV channel count or sample rate")

        total_samples = 0
        total_frames = 0
        sum_squares = 0
        peak_abs = 0
        nonzero_samples = 0
        significant_samples = 0
        clipped_samples = 0
        stereo_mismatch_frames = 0 if channels == 2 else None
        first_signal_frame: int | None = None
        last_signal_frame: int | None = None

        while True:
            raw = wav.readframes(65536)
            if not raw:
                break
            samples = array("h")
            samples.frombytes(raw)
            if sys.byteorder == "big":
                samples.byteswap()
            if len(samples) % channels:
                raise ValueError("PCM chunk ends with a partial frame")

            chunk_frames = len(samples) // channels
            if stereo_mismatch_frames is not None:
                stereo_mismatch_frames += sum(
                    samples[index] != samples[index + 1]
                    for index in range(0, len(samples), 2)
                )

            for index, sample in enumerate(samples):
                magnitude = abs(sample)
                peak_abs = max(peak_abs, magnitude)
                sum_squares += sample * sample
                nonzero_samples += sample != 0
                clipped_samples += sample in (-32768, 32767)
                if magnitude >= significant_threshold:
                    significant_samples += 1
                    frame = total_frames + index // channels
                    if first_signal_frame is None:
                        first_signal_frame = frame
                    last_signal_frame = frame

            total_samples += len(samples)
            total_frames += chunk_frames

    if total_frames != declared_frames:
        raise ValueError(
            f"read {total_frames} PCM frames, header declares {declared_frames}"
        )
    if total_samples == 0:
        raise ValueError("WAV capture has no PCM samples")

    first_signal_seconds = (
        first_signal_frame / sample_rate if first_signal_frame is not None else None
    )
    last_signal_seconds = (
        last_signal_frame / sample_rate if last_signal_frame is not None else None
    )
    active_span_seconds = 0.0
    if first_signal_frame is not None and last_signal_frame is not None:
        active_span_seconds = (last_signal_frame - first_signal_frame + 1) / sample_rate

    return PcmWavAnalysis(
        path=str(path.resolve()),
        channels=channels,
        sample_rate_hz=sample_rate,
        bits_per_sample=sample_width * 8,
        frames=total_frames,
        data_bytes=total_samples * sample_width,
        duration_seconds=total_frames / sample_rate,
        peak_abs=peak_abs,
        rms=math.sqrt(sum_squares / total_samples),
        nonzero_ratio=nonzero_samples / total_samples,
        significant_ratio=significant_samples / total_samples,
        clipped_samples=clipped_samples,
        stereo_mismatch_frames=stereo_mismatch_frames,
        first_signal_seconds=first_signal_seconds,
        last_signal_seconds=last_signal_seconds,
        active_span_seconds=active_span_seconds,
    )


def validate_audio_regression(
    analysis: PcmWavAnalysis,
    *,
    expected_sample_rate: int,
    min_duration_seconds: float = 5.0,
    min_peak_abs: int = 128,
    min_significant_ratio: float = 0.05,
    max_clipped_samples: int = 0,
    require_mono_stereo_copy: bool = True,
) -> None:
    failures: list[str] = []
    if analysis.sample_rate_hz != expected_sample_rate:
        failures.append(
            f"sample rate {analysis.sample_rate_hz}, expected {expected_sample_rate}"
        )
    if analysis.duration_seconds < min_duration_seconds:
        failures.append(
            f"duration {analysis.duration_seconds:.3f}s < {min_duration_seconds:.3f}s"
        )
    if analysis.peak_abs < min_peak_abs:
        failures.append(f"peak {analysis.peak_abs} < {min_peak_abs}")
    if analysis.significant_ratio < min_significant_ratio:
        failures.append(
            f"significant ratio {analysis.significant_ratio:.6f} < "
            f"{min_significant_ratio:.6f}"
        )
    if analysis.clipped_samples > max_clipped_samples:
        failures.append(
            f"clipped samples {analysis.clipped_samples} > {max_clipped_samples}"
        )
    if require_mono_stereo_copy and analysis.stereo_mismatch_frames != 0:
        failures.append(
            f"stereo mismatch frames {analysis.stereo_mismatch_frames}; expected mono copy"
        )
    if failures:
        raise AssertionError("; ".join(failures))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav", type=Path)
    parser.add_argument(
        "--expected-rate",
        type=int,
        required=True,
        help="Expected guest sample rate in Hz",
    )
    parser.add_argument("--min-duration", type=float, default=5.0)
    parser.add_argument("--min-peak", type=int, default=128)
    parser.add_argument("--min-significant-ratio", type=float, default=0.05)
    parser.add_argument(
        "--allow-stereo-difference",
        action="store_true",
        help="Do not require identical left/right samples",
    )
    args = parser.parse_args(argv)

    repaired = finalize_qemu_wav_header(args.wav)
    analysis = analyze_pcm_wav(args.wav)
    validate_audio_regression(
        analysis,
        expected_sample_rate=args.expected_rate,
        min_duration_seconds=args.min_duration,
        min_peak_abs=args.min_peak,
        min_significant_ratio=args.min_significant_ratio,
        require_mono_stereo_copy=not args.allow_stereo_difference,
    )
    result = analysis.to_dict()
    result["header_repaired"] = repaired
    result["passed"] = True
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
