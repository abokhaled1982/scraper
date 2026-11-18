"""Find the most clip-worthy moments.

LLM path (local Qwen3, no API): the transcript is shown as numbered, time-stamped sentences in overlapping
windows; the model returns JSON sentence ranges + score/title/hook. Its picks are blended with heuristic
audio/text signals, forced into the duration limits, snapped to clean cut points and de-duplicated.
Heuristic path: every valid sentence window is scored by `SignalModel` alone.
"""

from __future__ import annotations

import json
import logging
import re

import numpy as np

from .config import LANGUAGES, ClipperConfig
from .features import SignalModel
from .models import Moment, Sentence, Transcript
from .refine import snap_bounds
from .segment import candidate_windows, fit_range, split_sentences, trim_words_to

log = logging.getLogger("clipper.scorer")

LLM_WEIGHT = 0.75

SYSTEM_PROMPT = (
    "You are a senior short-form video editor for TikTok, Instagram Reels and YouTube Shorts. "
    "You find the moments in long videos that will perform best as standalone vertical clips. "
    "You are strict, calibrated and answer only with valid JSON."
)

USER_PROMPT = """Transcript excerpt (language: {language}). Each line: [sentence_id] mm:ss | text

{lines}

Pick up to {k} non-overlapping clips that would go viral as standalone short videos.
Rules:
- A clip is a contiguous sentence range start_id..end_id lasting {min_sec:.0f}-{max_sec:.0f} seconds (ideal ~{target_sec:.0f}s).
- The FIRST sentence must hook instantly (bold claim, question, surprising fact, conflict, strong emotion) and be understandable without earlier context (no dangling "and so / he / that").
- The clip needs a payoff (answer, punchline, insight, emotional peak) and must end on a complete thought.
- Prefer controversial opinions, surprising stories, practical tips, funny or emotional moments, quotable lines.
- Avoid intros, outros, sponsor reads, small talk and moments that depend on visuals.
- score: viral potential 0-100. Be strict: average content 40-60, only exceptional moments above 80.
- title: catchy title (max 8 words) in {language}. hook: on-screen text for the first seconds (max 8 words) in {language}.
Return ONLY a JSON array, no prose, e.g.
[{{"start_id": 12, "end_id": 19, "score": 78, "title": "...", "hook": "...", "reason": "..."}}]
Return [] if nothing is worth clipping."""


def _mmss(t: float) -> str:
    return f"{int(t // 60):02d}:{int(t % 60):02d}"


def _windows(sentences: list[Sentence], max_words: int, overlap: float = 0.15) -> list[tuple[int, int]]:
    out, i, n = [], 0, len(sentences)
    while i < n:
        words, j = 0, i
        while j < n and (words == 0 or words + (sentences[j].last_word - sentences[j].first_word + 1) <= max_words):
            words += sentences[j].last_word - sentences[j].first_word + 1
            j += 1
        out.append((i, j - 1))
        if j >= n:
            break
        i = max(i + 1, j - int((j - i) * overlap))
    return out


def parse_json_array(text: str) -> list[dict]:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return []
    blob = text[start : end + 1]
    for candidate in (blob, re.sub(r",\s*([\]}])", r"\1", blob)):
        try:
            data = json.loads(candidate)
            return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
        except json.JSONDecodeError:
            continue
    return []


class LocalLLM:
    def __init__(self, cfg: ClipperConfig):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        if not torch.cuda.is_available():
            raise RuntimeError("LLM scoring needs a GPU")
        major, _ = torch.cuda.get_device_capability(0)
        dtype = torch.bfloat16 if major >= 8 else torch.float16
        kwargs: dict = {"torch_dtype": dtype, "device_map": "auto"}
        if cfg.llm_4bit:
            from transformers import BitsAndBytesConfig

            kwargs["quantization_config"] = BitsAndBytesConfig(
                load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=dtype
            )
        log.info("Loading %s%s", cfg.llm_model, " (4-bit)" if cfg.llm_4bit else "")
        self.tokenizer = AutoTokenizer.from_pretrained(cfg.llm_model)
        self.model = AutoModelForCausalLM.from_pretrained(cfg.llm_model, **kwargs).eval()
        self.thinking = cfg.llm_thinking

    def chat(self, system: str, user: str) -> str:
        import torch

        prompt = self.tokenizer.apply_chat_template(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=self.thinking,
        )
        inputs = self.tokenizer(prompt, return_tensors="pt").to(self.model.device)
        with torch.inference_mode():
            out = self.model.generate(
                **inputs,
                max_new_tokens=4096 if self.thinking else 1200,
                do_sample=False,
                repetition_penalty=1.05,
            )
        return self.tokenizer.decode(out[0, inputs["input_ids"].shape[1] :], skip_special_tokens=True)

    def close(self) -> None:
        del self.model
        from .transcribe import free_gpu

        free_gpu()


def llm_candidates(transcript: Transcript, sentences: list[Sentence], cfg: ClipperConfig) -> list[dict]:
    llm = LocalLLM(cfg)
    language = LANGUAGES.get(transcript.language or "", ("the transcript's language",))[0]
    picks: list[dict] = []
    try:
        windows = _windows(sentences, cfg.llm_window_words)
        per_window = min(6, max(3, int(np.ceil(2 * cfg.max_clips / max(1, len(windows))))))
        for n, (a, b) in enumerate(windows, 1):
            lines = "\n".join(f"[{s.id}] {_mmss(s.start)} | {s.text}" for s in sentences[a : b + 1])
            user = USER_PROMPT.format(
                language=language, lines=lines, k=per_window,
                min_sec=cfg.min_clip_sec, max_sec=cfg.max_clip_sec, target_sec=cfg.target_clip_sec,
            )
            raw = llm.chat(SYSTEM_PROMPT, user)
            items = parse_json_array(raw)
            if not items and raw.strip() not in ("[]", ""):
                raw = llm.chat(SYSTEM_PROMPT, user + "\n\nYour previous answer was not valid JSON. Output the JSON array only.")
                items = parse_json_array(raw)
            for it in items:
                try:
                    i, j = int(it["start_id"]), int(it["end_id"])
                except (KeyError, TypeError, ValueError):
                    continue
                if not (a <= i <= b) or j < i:
                    continue
                picks.append({
                    "i": i, "j": min(j, len(sentences) - 1),
                    "score": float(np.clip(float(it.get("score", 50) or 50), 0, 100)),
                    "title": str(it.get("title", "")).strip(),
                    "hook": str(it.get("hook", "")).strip(),
                    "reason": str(it.get("reason", "")).strip(),
                })
            log.info("LLM window %d/%d -> %d picks", n, len(windows), len(items))
    finally:
        llm.close()
    return picks


def _iou(a: Moment, b: Moment) -> float:
    inter = max(0.0, min(a.end, b.end) - max(a.start, b.start))
    union = max(a.end, b.end) - min(a.start, b.start)
    return inter / union if union > 0 else 0.0


def select(moments: list[Moment], k: int, max_overlap: float) -> list[Moment]:
    chosen: list[Moment] = []
    for m in sorted(moments, key=lambda m: m.score, reverse=True):
        if all(_iou(m, c) <= max_overlap for c in chosen):
            chosen.append(m)
        if len(chosen) >= k:
            break
    return chosen


def detect_moments(transcript: Transcript, env_db: np.ndarray, cfg: ClipperConfig) -> list[Moment]:
    words = transcript.words
    sentences = split_sentences(words)
    if not sentences:
        return []
    signals = SignalModel(transcript, sentences, env_db)

    def build(i: int, j: int, **meta) -> Moment:
        i, j = fit_range(sentences, i, j, cfg.min_clip_sec, cfg.max_clip_sec)
        first, last = sentences[i].first_word, sentences[j].last_word
        last = trim_words_to(words, first, last, cfg.max_clip_sec)
        h_score, _ = signals.window_score(sentences[i : j + 1], cfg.target_clip_sec)
        start, end = snap_bounds(words, first, last, env_db, transcript.duration, cfg.pad_start, cfg.pad_end)
        llm_score = meta.pop("llm_score", None)
        score = h_score if llm_score is None else LLM_WEIGHT * llm_score + (1 - LLM_WEIGHT) * h_score
        return Moment(start=start, end=end, first_word=first, last_word=last, score=round(score, 1),
                      llm_score=llm_score, heuristic_score=round(h_score, 1), **meta)

    moments: list[Moment] = []
    if cfg.use_llm:
        try:
            for p in llm_candidates(transcript, sentences, cfg):
                moments.append(build(p["i"], p["j"], llm_score=p["score"], title=p["title"], hook=p["hook"],
                                     reason=p["reason"], source="llm"))
        except Exception as exc:
            log.warning("LLM scoring unavailable (%s) - using heuristic scoring only", exc)

    if len(select(moments, cfg.max_clips, cfg.max_overlap)) < cfg.max_clips:
        windows = candidate_windows(sentences, cfg.min_clip_sec, cfg.max_clip_sec)
        if not windows:  # very short input: take everything that fits
            windows = [(0, len(sentences) - 1)]
        scored = sorted(
            ((signals.window_score(sentences[i : j + 1], cfg.target_clip_sec)[0], i, j) for i, j in windows),
            reverse=True,
        )
        # Fill remaining slots with heuristic picks, ranked below any LLM pick.
        penalty = 0.85 if moments else 1.0
        for h, i, j in scored[: max(50, 10 * cfg.max_clips)]:
            m = build(i, j, title=sentences[i].text[:60], hook="", reason="heuristic", source="heuristic")
            m.score = round(m.score * penalty, 1)
            moments.append(m)

    chosen = select(moments, cfg.max_clips, cfg.max_overlap)
    return sorted(chosen, key=lambda m: m.score, reverse=True)
