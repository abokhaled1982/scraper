"""Single-pass, frame-accurate clip rendering (cut + reframe + burned captions + audio polish)."""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from .config import ClipperConfig
from .media import FFmpegError, VideoInfo
from .reframe import CropPlan

log = logging.getLogger("clipper.render")


def output_fps(info: VideoInfo, cfg: ClipperConfig) -> float:
    fps = cfg.fps or info.fps or 30.0
    return round(min(max(fps, 15.0), 60.0), 3)


def _escape(path: str) -> str:
    return path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def _audio_chain(duration: float) -> str:
    fade_out = max(0.0, duration - 0.08)
    return (
        "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,"
        f"afade=t=in:st=0:d=0.03,afade=t=out:st={fade_out:.3f}:d=0.08"
    )


def _geometry(plan: CropPlan) -> str:
    if plan.mode == "fit":
        w, h = plan.out_w, plan.out_h
        return (
            f"split[bgsrc][fgsrc];"
            f"[bgsrc]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=24:4,eq=brightness=-0.06[bg];"
            f"[fgsrc]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1"
        )
    if plan.mode == "crop":
        x = int(plan.xs[0]) if plan.xs is not None and len(plan.xs) else 0
        return f"crop={plan.crop_w}:{plan.crop_h}:{x}:{plan.crop_y},scale={plan.out_w}:{plan.out_h}:flags=lanczos,setsar=1"
    return f"scale={plan.out_w}:{plan.out_h},setsar=1"


def render_clip(
    video: str,
    info: VideoInfo,
    start: float,
    end: float,
    plan: CropPlan,
    ass_text: str | None,
    out_path: Path,
    cfg: ClipperConfig,
) -> None:
    duration = end - start
    fps = output_fps(info, cfg)
    video = str(Path(video).resolve())
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="clipper_") as tmp:
        subs = ""
        if ass_text:
            (Path(tmp) / "subs.ass").write_text(ass_text, encoding="utf-8")
            subs = ",ass=subs.ass" + (f":fontsdir='{_escape(str(Path(cfg.fonts_dir).resolve()))}'" if cfg.fonts_dir else "")
        encode = ["-c:v", "libx264", "-preset", cfg.preset, "-crf", str(cfg.crf), "-pix_fmt", "yuv420p",
                  "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-t", f"{duration:.3f}", str(out_path)]

        if plan.static:
            graph = f"[0:v]fps={fps},{_geometry(plan)}{subs}[v];[0:a]{_audio_chain(duration)}[a]"
            cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", video, "-t", f"{duration:.3f}",
                   "-filter_complex", graph, "-map", "[v]", "-map", "[a]", *encode]
            proc = subprocess.run(cmd, cwd=tmp, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if proc.returncode != 0:
                raise FFmpegError("Render failed:\n" + proc.stderr.decode(errors="replace")[-1500:])
            return
        _render_dynamic_crop(video, info, start, duration, fps, plan, subs, encode, Path(tmp))


def _render_dynamic_crop(video, info, start, duration, fps, plan, subs, encode, tmp: Path) -> None:
    W, H = info.width, info.height
    decode = ["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{start:.3f}", "-i", video, "-t", f"{duration:.3f}",
              "-vf", f"fps={fps}", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
    graph = (f"[0:v]scale={plan.out_w}:{plan.out_h}:flags=lanczos,setsar=1{subs}[v];"
             f"[1:a]{_audio_chain(duration)}[a]")
    enc = ["ffmpeg", "-nostdin", "-y", "-v", "error",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{plan.crop_w}x{plan.crop_h}", "-r", str(fps), "-i", "pipe:0",
           "-ss", f"{start:.3f}", "-i", video, "-t", f"{duration:.3f}",
           "-filter_complex", graph, "-map", "[v]", "-map", "[a]", *encode]

    err_path = tmp / "encoder.log"
    with open(err_path, "wb") as err:
        dec = subprocess.Popen(decode, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        encp = subprocess.Popen(enc, stdin=subprocess.PIPE, stderr=err, cwd=tmp)
        frame_bytes = W * H * 3
        xs = plan.xs
        k = 0
        try:
            while True:
                buf = dec.stdout.read(frame_bytes)
                if len(buf) < frame_bytes:
                    break
                frame = np.frombuffer(buf, np.uint8).reshape(H, W, 3)
                x = int(xs[min(k, len(xs) - 1)])
                crop = frame[plan.crop_y : plan.crop_y + plan.crop_h, x : x + plan.crop_w]
                encp.stdin.write(np.ascontiguousarray(crop).tobytes())
                k += 1
        except BrokenPipeError:
            pass
        finally:
            dec.stdout.close()
            dec.wait()
            try:
                encp.stdin.close()
            except BrokenPipeError:
                pass
            encp.wait()
    if encp.returncode != 0 or k == 0:
        raise FFmpegError(f"Dynamic-crop render failed after {k} frames:\n" + err_path.read_text(errors="replace")[-1500:])


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


__all__ = ["render_clip", "output_fps", "ffmpeg_available"]
