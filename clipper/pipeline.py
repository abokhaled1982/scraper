from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import asdict
from pathlib import Path

from .captions import build_ass, build_srt, clip_words
from .config import ClipperConfig
from .media import load_audio, probe, rms_envelope
from .models import Moment, Transcript, load_moments, save_moments
from .reframe import plan_crop
from .render import output_fps, render_clip

log = logging.getLogger("clipper")


def slugify(text: str, limit: int = 40) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[^\w\s-]", "", text, flags=re.U).strip().lower()
    return re.sub(r"[\s_-]+", "-", text)[:limit].strip("-") or "clip"


class Pipeline:
    def __init__(self, video: str | Path, out_dir: str | Path, cfg: ClipperConfig):
        self.video = str(Path(video).resolve())
        self.cfg = cfg
        self.work = Path(out_dir) / slugify(Path(video).stem, 60)
        self.work.mkdir(parents=True, exist_ok=True)
        self.transcript_path = self.work / "transcript.json"
        self.moments_path = self.work / "moments.json"
        self.info = probe(self.video)
        self._env = None

    @property
    def env_db(self):
        if self._env is None:
            self._env = rms_envelope(load_audio(self.video))
        return self._env

    def transcribe(self, force: bool = False) -> Transcript:
        if self.transcript_path.exists() and not force:
            log.info("Using cached %s", self.transcript_path)
            return Transcript.load(self.transcript_path)
        if self.cfg.asr_url:
            from .remote import transcribe_remote

            transcript = transcribe_remote(self.video, self.cfg)
        else:
            from .transcribe import transcribe

            transcript = transcribe(self.video, self.cfg)
        if self.cfg.llm_url and self.cfg.llm_correct:
            from .remote import correct_transcript

            transcript.save(self.work / "transcript.raw.json")
            transcript, changes = correct_transcript(transcript, self.cfg)
            (self.work / "corrections.json").write_text(json.dumps(changes, ensure_ascii=False, indent=1), encoding="utf-8")
        transcript.save(self.transcript_path)
        (self.work / "transcript.txt").write_text(transcript.text, encoding="utf-8")
        log.info("Transcript: %d words, language=%s", len(transcript.words), transcript.language)
        return transcript

    def detect(self, transcript: Transcript, force: bool = False) -> list[Moment]:
        if self.moments_path.exists() and not force:
            log.info("Using cached %s (edit it and re-run `render` to adjust clips)", self.moments_path)
            return load_moments(self.moments_path)
        from .scorer import detect_moments

        moments = detect_moments(transcript, self.env_db, self.cfg)
        save_moments(moments, self.moments_path)
        return moments

    def render(self, transcript: Transcript, moments: list[Moment]) -> list[Path]:
        clips_dir = self.work / "clips"
        clips_dir.mkdir(exist_ok=True)
        fps = output_fps(self.info, self.cfg)
        outputs = []
        for n, m in enumerate(moments, 1):
            name = f"{n:02d}_{slugify(m.title or 'clip')}"
            duration = m.end - m.start
            log.info("[%d/%d] %s  %.2f-%.2fs (%.1fs) score=%.0f", n, len(moments), name, m.start, m.end, duration, m.score)
            plan = plan_crop(self.video, self.info, m.start, duration, fps, self.cfg.reframe, self.cfg.out_width, self.cfg.out_height)
            words = clip_words(transcript.words, m.start, m.end)
            title = (m.hook or m.title) if self.cfg.hook_title else ""
            ass = None
            if self.cfg.captions != "none" or title:
                ass = build_ass(words, self.cfg, transcript.language, plan.out_w, plan.out_h, duration, title)
                (clips_dir / f"{name}.ass").write_text(ass, encoding="utf-8")
            (clips_dir / f"{name}.srt").write_text(build_srt(words, transcript.language), encoding="utf-8")
            out = clips_dir / f"{name}.mp4"
            render_clip(self.video, self.info, m.start, m.end, plan, ass, out, self.cfg)
            outputs.append(out)
        summary = [{**asdict(m), "file": str(p.name), "text": " ".join(w.text for w in clip_words(transcript.words, m.start, m.end))}
                   for m, p in zip(moments, outputs)]
        (self.work / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
        return outputs

    def run(self, force: bool = False, redetect: bool = False) -> list[Path]:
        transcript = self.transcribe(force)
        moments = self.detect(transcript, force or redetect)
        if not moments:
            log.warning("No moments found")
            return []
        return self.render(transcript, moments)
