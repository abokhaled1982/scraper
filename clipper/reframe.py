"""Vertical 9:16 reframing: face tracking -> per-shot smoothed virtual camera path."""

from __future__ import annotations

import logging
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .media import VideoInfo

log = logging.getLogger("clipper.reframe")

YUNET_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
YUNET_PATH = Path.home() / ".cache" / "clipper" / "face_detection_yunet_2023mar.onnx"
SAMPLE_FPS = 5.0
DETECT_WIDTH = 640


@dataclass
class CropPlan:
    mode: str  # crop | fit | none
    out_w: int
    out_h: int
    crop_w: int = 0
    crop_h: int = 0
    crop_y: int = 0
    xs: np.ndarray | None = None  # left x per output frame (crop mode)
    face_coverage: float = 0.0

    @property
    def static(self) -> bool:
        return self.mode != "crop" or self.xs is None or len(np.unique(self.xs)) <= 1


class FaceDetector:
    def __init__(self) -> None:
        import cv2

        self.cv2 = cv2
        self.yunet = None
        try:
            if not YUNET_PATH.exists():
                YUNET_PATH.parent.mkdir(parents=True, exist_ok=True)
                partial = YUNET_PATH.with_suffix(".part")
                urllib.request.urlretrieve(YUNET_URL, partial)
                partial.replace(YUNET_PATH)
            self.yunet = cv2.FaceDetectorYN.create(str(YUNET_PATH), "", (320, 320), 0.7, 0.3, 50)
        except Exception as exc:
            log.warning("YuNet unavailable (%s); using Haar cascade", exc)
            self.haar = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")

    def __call__(self, frame: np.ndarray) -> list[tuple[float, float, float, float]]:
        """Return faces as (center_x, center_y, width, score) in pixels of `frame`."""
        h, w = frame.shape[:2]
        if self.yunet is not None:
            self.yunet.setInputSize((w, h))
            _, faces = self.yunet.detect(frame)
            if faces is None:
                return []
            return [(f[0] + f[2] / 2, f[1] + f[3] / 2, f[2], float(f[14])) for f in faces]
        gray = self.cv2.cvtColor(frame, self.cv2.COLOR_BGR2GRAY)
        found = self.haar.detectMultiScale(gray, 1.1, 5, minSize=(int(w * 0.04), int(w * 0.04)))
        return [(x + fw / 2, y + fh / 2, fw, 1.0) for x, y, fw, fh in found]


def _decode_samples(video: str, start: float, duration: float, width: int, height: int):
    import cv2  # noqa: F401  (fail early if OpenCV is missing)

    dh = int(round(DETECT_WIDTH * height / width / 2) * 2)
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-ss", f"{start:.3f}", "-i", video, "-t", f"{duration:.3f}",
        "-vf", f"fps={SAMPLE_FPS},scale={DETECT_WIDTH}:{dh}", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size = DETECT_WIDTH * dh * 3
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield np.frombuffer(buf, np.uint8).reshape(dh, DETECT_WIDTH, 3)
    finally:
        proc.stdout.close()
        proc.wait()


def _shot_ids(hists: list[np.ndarray], threshold: float = 0.55) -> np.ndarray:
    import cv2

    ids = np.zeros(len(hists), dtype=int)
    for k in range(1, len(hists)):
        corr = cv2.compareHist(hists[k - 1], hists[k], cv2.HISTCMP_CORREL)
        ids[k] = ids[k - 1] + (1 if corr < threshold else 0)
    return ids


def _choose_targets(detections: list[list[tuple]], frame_w: int, crop_frac: float) -> np.ndarray:
    """Pick one horizontal target (0..1) per sample, sticking to the current subject."""
    targets = np.full(len(detections), np.nan)
    prev = np.nan
    for k, faces in enumerate(detections):
        faces = [f for f in faces if f[2] >= 0.035 * frame_w]
        if not faces:
            continue
        largest = max(f[2] for f in faces)
        main = [f for f in faces if f[2] >= 0.5 * largest]
        xs = [f[0] / frame_w for f in main]
        if max(xs) - min(xs) <= crop_frac * 0.7:
            target = (max(xs) + min(xs)) / 2  # everyone fits in frame
        elif not np.isnan(prev):
            near = min(main, key=lambda f: abs(f[0] / frame_w - prev))
            biggest = max(main, key=lambda f: f[2])
            # Switch subject only if the other face is clearly dominant.
            target = (biggest if biggest[2] > 1.4 * near[2] else near)[0] / frame_w
        else:
            target = max(main, key=lambda f: f[2])[0] / frame_w
        targets[k] = prev = target
    return targets


def _smooth_shot(series: np.ndarray, dead_zone: float) -> np.ndarray:
    valid = ~np.isnan(series)
    if not valid.any():
        return np.full_like(series, 0.5)
    idx = np.arange(len(series))
    filled = np.interp(idx, idx[valid], series[valid])
    if len(filled) >= 5:
        pad = np.pad(filled, 2, mode="edge")
        filled = np.median(np.lib.stride_tricks.sliding_window_view(pad, 5), axis=1)
    if filled.max() - filled.min() <= dead_zone:
        return np.full_like(filled, float(np.median(filled)))  # locked-off camera
    cam, out = filled[0], np.empty_like(filled)
    for k, p in enumerate(filled):
        diff = p - cam
        if abs(diff) > dead_zone:
            cam += diff - np.sign(diff) * dead_zone
        out[k] = cam
    win = int(SAMPLE_FPS)  # ~1 s moving average for a soft pan
    if len(out) > win:
        kernel = np.ones(win) / win
        out = np.convolve(np.pad(out, (win // 2, win - 1 - win // 2), mode="edge"), kernel, mode="valid")
    return out


def plan_crop(video: str, info: VideoInfo, start: float, duration: float, fps: float, mode: str, out_w: int, out_h: int) -> CropPlan:
    W, H = info.width, info.height
    if mode == "none":
        return CropPlan("none", W - W % 2, H - H % 2)
    aspect = out_w / out_h
    if mode == "fit" or W / H <= aspect * 1.15:  # already (near) vertical: nothing to crop away
        return CropPlan("fit", out_w, out_h)

    crop_h = H - H % 2
    crop_w = int(round(crop_h * aspect / 2) * 2)
    if crop_w > W:
        crop_w = W - W % 2
        crop_h = int(round(crop_w / aspect / 2) * 2)
    crop_y = (H - crop_h) // 2
    n_frames = max(1, int(round(duration * fps)))

    if mode == "center":
        return CropPlan("crop", out_w, out_h, crop_w, crop_h, crop_y, np.full(n_frames, (W - crop_w) // 2 // 2 * 2))

    import cv2

    detector = FaceDetector()
    detections, hists = [], []
    for frame in _decode_samples(video, start, duration, W, H):
        detections.append(detector(frame))
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [32, 32], [0, 180, 0, 256])
        hists.append(cv2.normalize(hist, hist).flatten())
    if not detections:
        return CropPlan("fit", out_w, out_h)

    coverage = sum(bool(d) for d in detections) / len(detections)
    if mode == "auto" and coverage < 0.3:
        log.info("Faces in %.0f%% of samples -> blurred-background fit", 100 * coverage)
        return CropPlan("fit", out_w, out_h, face_coverage=coverage)

    det_w = DETECT_WIDTH
    targets = _choose_targets(detections, det_w, crop_w / W)
    shots = _shot_ids(hists)
    path = np.empty(len(targets))
    for sid in np.unique(shots):
        sel = shots == sid
        path[sel] = _smooth_shot(targets[sel], dead_zone=0.06)

    # Map sample path -> every output frame (hard cuts at shot changes, interpolation inside shots).
    sample_t = np.arange(len(path)) / SAMPLE_FPS
    frame_t = np.arange(n_frames) / fps
    centers = np.empty(n_frames)
    k = np.clip(np.searchsorted(sample_t, frame_t, side="right") - 1, 0, len(path) - 1)
    for i, t in enumerate(frame_t):
        j = k[i]
        if j + 1 < len(path) and shots[j + 1] == shots[j]:
            a = (t - sample_t[j]) * SAMPLE_FPS
            centers[i] = path[j] * (1 - a) + path[j + 1] * a
        else:
            centers[i] = path[j]
    xs = np.clip(np.round(centers * W - crop_w / 2), 0, W - crop_w).astype(int) // 2 * 2
    return CropPlan("crop", out_w, out_h, crop_w, crop_h, crop_y, xs, coverage)
