from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000


class FFmpegError(RuntimeError):
    pass


def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace").strip().splitlines()[-15:]
        raise FFmpegError(f"{cmd[0]} failed ({proc.returncode}):\n" + "\n".join(tail))


@dataclass
class VideoInfo:
    path: str
    duration: float
    width: int  # display width (after rotation)
    height: int
    fps: float
    has_audio: bool
    has_video: bool


def _parse_rate(rate: str | None) -> float:
    if not rate or rate in ("0/0", "0"):
        return 0.0
    if "/" in rate:
        num, den = rate.split("/", 1)
        return float(num) / float(den) if float(den) else 0.0
    return float(rate)


def probe(path: str | Path) -> VideoInfo:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe cannot read {path}: {proc.stderr.decode(errors='replace')[-500:]}")
    data = json.loads(proc.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = float(data.get("format", {}).get("duration") or 0.0)

    width = height = 0
    fps = 0.0
    if video:
        width, height = int(video.get("width", 0)), int(video.get("height", 0))
        rotation = 0
        for side in video.get("side_data_list", []) or []:
            if "rotation" in side:
                rotation = int(float(side["rotation"]))
        rotation = rotation or int(float(video.get("tags", {}).get("rotate", 0) or 0))
        if abs(rotation) % 180 == 90:
            width, height = height, width
        fps = _parse_rate(video.get("avg_frame_rate")) or _parse_rate(video.get("r_frame_rate"))
        if not duration:
            duration = float(video.get("duration") or 0.0)
    return VideoInfo(str(path), duration, width, height, fps or 30.0, audio is not None, video is not None)


def load_audio(path: str | Path, sr: int = SAMPLE_RATE, start: float | None = None, duration: float | None = None) -> np.ndarray:
    """Decode audio to mono float32 PCM at `sr` Hz."""
    cmd = ["ffmpeg", "-nostdin", "-v", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(path)]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "pipe:1"]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise FFmpegError(f"Audio decode failed: {proc.stderr.decode(errors='replace')[-500:]}")
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def rms_envelope(audio: np.ndarray, sr: int = SAMPLE_RATE, hop: float = 0.01) -> np.ndarray:
    """RMS loudness in dBFS per `hop` seconds frame."""
    step = max(1, int(sr * hop))
    n = len(audio) // step
    if n == 0:
        return np.full(1, -100.0, dtype=np.float32)
    frames = audio[: n * step].reshape(n, step)
    rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1) + 1e-12)
    return (20 * np.log10(rms + 1e-9)).astype(np.float32)
