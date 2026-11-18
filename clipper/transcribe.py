"""Speech -> word-timestamped transcript.

1. Silero VAD finds speech; speech is packed into <= `chunk_max_sec` chunks that start/end in silence
   (so no word is ever split between chunks and chunk texts can be joined with spaces).
2. Qwen3-ASR transcribes all chunks in batches (language forced or auto-detected per chunk).
3. Words get timestamps from Qwen3-ForcedAligner (de/en/fr/es/...) or the MMS CTC aligner
   (Arabic and every language Qwen's aligner does not cover). Misses are interpolated.
"""

from __future__ import annotations

import gc
import logging
import re
from collections import Counter
from dataclasses import dataclass

import numpy as np

from .config import LANGUAGES, QWEN_ALIGNER_LANGS, ClipperConfig, language_code, qwen_language_name
from .media import SAMPLE_RATE, load_audio, probe, rms_envelope
from .models import Transcript, Word
from .textalign import attach_times, interpolate_words, normalize, tokenize

log = logging.getLogger("clipper.transcribe")


@dataclass
class Chunk:
    start: float
    end: float
    text: str = ""
    language: str | None = None


# ---------------------------------------------------------------- VAD + chunking

def detect_speech(audio: np.ndarray, threshold: float) -> list[tuple[float, float]]:
    import torch
    from silero_vad import get_speech_timestamps, load_silero_vad

    model = load_silero_vad()
    stamps = get_speech_timestamps(
        torch.from_numpy(audio),
        model,
        threshold=threshold,
        sampling_rate=SAMPLE_RATE,
        min_speech_duration_ms=200,
        min_silence_duration_ms=250,
        speech_pad_ms=80,
    )
    return [(s["start"] / SAMPLE_RATE, s["end"] / SAMPLE_RATE) for s in stamps]


def _split_long(seg: tuple[float, float], max_sec: float, env_db: np.ndarray, hop: float) -> list[tuple[float, float]]:
    """Split a speech region longer than max_sec at its quietest points."""
    out, start, end = [], seg[0], seg[1]
    while end - start > max_sec:
        lo, hi = start + max_sec * 0.6, start + max_sec
        i0, i1 = int(lo / hop), max(int(lo / hop) + 1, int(hi / hop))
        window = env_db[i0:i1]
        cut = (i0 + int(np.argmin(window))) * hop if len(window) else hi
        out.append((start, cut))
        start = cut
    out.append((start, end))
    return out


def plan_chunks(speech: list[tuple[float, float]], env_db: np.ndarray, max_sec: float, hop: float = 0.01, max_gap: float = 3.0) -> list[Chunk]:
    pieces: list[tuple[float, float]] = []
    for seg in speech:
        pieces.extend(_split_long(seg, max_sec, env_db, hop))
    chunks: list[Chunk] = []
    for s, e in pieces:
        if chunks and e - chunks[-1].start <= max_sec and s - chunks[-1].end <= max_gap:
            chunks[-1].end = e
        else:
            chunks.append(Chunk(s, e))
    return chunks


# ---------------------------------------------------------------- models

def _device_dtype():
    import torch

    if not torch.cuda.is_available():
        log.warning("No CUDA GPU found - running on CPU will be very slow.")
        return "cpu", torch.float32
    major, _ = torch.cuda.get_device_capability(0)
    return "cuda:0", (torch.bfloat16 if major >= 8 else torch.float16)


def free_gpu() -> None:
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass


_REPEAT = re.compile(r"(\b\S+\b)(?:\s+\1\b){4,}")


def _clean_text(text: str) -> str:
    # Guard against ASR loops on music/noise ("la la la la la ...").
    text = _REPEAT.sub(lambda m: f"{m.group(1)} {m.group(1)}", text)
    return re.sub(r"\s+", " ", text).strip()


def run_asr(cfg: ClipperConfig, audio: np.ndarray, chunks: list[Chunk]) -> None:
    from qwen_asr import Qwen3ASRModel

    device, dtype = _device_dtype()
    log.info("Loading %s on %s", cfg.asr_model, device)
    model = Qwen3ASRModel.from_pretrained(
        cfg.asr_model,
        dtype=dtype,
        device_map=device,
        max_inference_batch_size=cfg.asr_batch_size,
        max_new_tokens=1024,
    )
    forced = qwen_language_name(cfg.language)
    batch = max(1, cfg.asr_batch_size)
    for b in range(0, len(chunks), batch):
        group = chunks[b : b + batch]
        wavs = [(audio[int(c.start * SAMPLE_RATE) : int(c.end * SAMPLE_RATE)], SAMPLE_RATE) for c in group]
        results = model.transcribe(
            audio=wavs,
            language=[forced] * len(group),
            context=[cfg.asr_context] * len(group),
        )
        for chunk, res in zip(group, results):
            chunk.text = _clean_text(res.text or "")
            chunk.language = language_code(cfg.language) if forced else language_code(res.language)
        log.info("ASR %d/%d chunks", min(b + batch, len(chunks)), len(chunks))
    del model
    free_gpu()


def _items(result) -> list:
    return list(getattr(result, "items", result))


def align_qwen(cfg: ClipperConfig, audio: np.ndarray, chunks: list[Chunk]) -> dict[int, list[tuple[str, float, float]]]:
    from qwen_asr import Qwen3ForcedAligner

    device, dtype = _device_dtype()
    log.info("Loading %s for %d chunks", cfg.qwen_aligner_model, len(chunks))
    aligner = Qwen3ForcedAligner.from_pretrained(cfg.qwen_aligner_model, dtype=dtype, device_map=device)
    out: dict[int, list[tuple[str, float, float]]] = {}

    def align_group(model, group: list[Chunk]) -> None:
        results = model.align(
            audio=[(audio[int(c.start * SAMPLE_RATE) : int(c.end * SAMPLE_RATE)], SAMPLE_RATE) for c in group],
            text=[c.text for c in group],
            language=[LANGUAGES[c.language][0] for c in group],
        )
        for chunk, res in zip(group, results):
            out[id(chunk)] = [(it.text, chunk.start + float(it.start_time), chunk.start + float(it.end_time)) for it in _items(res)]

    batch = max(1, cfg.asr_batch_size)
    for b in range(0, len(chunks), batch):
        group = chunks[b : b + batch]
        try:
            align_group(aligner, group)
        except Exception as exc:
            # Retry one by one so a single bad chunk (or OOM on a padded batch) only costs that chunk.
            log.warning("Qwen aligner batch failed (%s); retrying chunk by chunk", exc)
            for chunk in group:
                try:
                    align_group(aligner, [chunk])
                except Exception as exc_one:
                    log.warning("Qwen aligner failed for chunk %.1f-%.1fs (%s); interpolating", chunk.start, chunk.end, exc_one)
                    free_gpu()
    del aligner
    free_gpu()
    return out


def align_ctc(cfg: ClipperConfig, audio: np.ndarray, chunks: list[Chunk]) -> dict[int, list[tuple[str, float, float]]]:
    import torch
    from ctc_forced_aligner import (
        generate_emissions,
        get_alignments,
        get_spans,
        load_alignment_model,
        postprocess_results,
        preprocess_text,
    )

    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.float16 if device == "cuda" else torch.float32
    log.info("Loading %s for %d chunks", cfg.ctc_aligner_model, len(chunks))
    model, tokenizer = load_alignment_model(device, cfg.ctc_aligner_model, dtype=dtype)
    out: dict[int, list[tuple[str, float, float]]] = {}
    for chunk in chunks:
        try:
            wav = torch.from_numpy(audio[int(chunk.start * SAMPLE_RATE) : int(chunk.end * SAMPLE_RATE)]).to(device=model.device, dtype=model.dtype)
            emissions, stride = generate_emissions(model, wav, batch_size=4)
            iso3 = LANGUAGES.get(chunk.language or "", ("", "eng", False))[1]
            # MMS normalisation deletes digit-only words (empty tokens break alignment); they get interpolated later.
            text = " ".join(t for t in tokenize(chunk.text) if not normalize(t).isdigit())
            if not text:
                continue
            tokens_starred, text_starred = preprocess_text(text, romanize=True, language=iso3)
            segments, scores, blank = get_alignments(emissions, tokens_starred, tokenizer)
            spans = get_spans(tokens_starred, segments, blank)
            stamps = postprocess_results(text_starred, spans, stride, scores)
            out[id(chunk)] = [(s["text"], chunk.start + float(s["start"]), chunk.start + float(s["end"])) for s in stamps]
        except Exception as exc:
            log.warning("CTC alignment failed for chunk %.1f-%.1fs (%s); interpolating", chunk.start, chunk.end, exc)
    del model
    free_gpu()
    return out


# ---------------------------------------------------------------- entry point

def transcribe(video: str, cfg: ClipperConfig) -> Transcript:
    info = probe(video)
    if not info.has_audio:
        raise RuntimeError(f"{video} has no audio track")
    log.info("Decoding audio (%.0fs)", info.duration)
    audio = load_audio(video)
    env_db = rms_envelope(audio)

    speech = detect_speech(audio, cfg.vad_threshold)
    if not speech:
        raise RuntimeError("No speech detected")
    chunks = plan_chunks(speech, env_db, cfg.chunk_max_sec)
    log.info("%d speech regions -> %d ASR chunks", len(speech), len(chunks))

    run_asr(cfg, audio, chunks)
    # Drop empty / punctuation-only outputs ("...", "♪"): nothing to caption, and they break aligners.
    chunks = [c for c in chunks if any(normalize(t) for t in tokenize(c.text))]
    if not chunks:
        raise RuntimeError("ASR produced no text")
    langs = Counter(c.language for c in chunks if c.language)
    dominant = language_code(cfg.language) if cfg.language != "auto" else (langs.most_common(1)[0][0] if langs else None)
    for c in chunks:
        if c.language not in LANGUAGES:
            c.language = dominant if dominant in LANGUAGES else "en"
    log.info("Language: %s (chunks: %s)", dominant, dict(langs))

    qwen_chunks = [c for c in chunks if c.language in QWEN_ALIGNER_LANGS]
    ctc_chunks = [c for c in chunks if c.language not in QWEN_ALIGNER_LANGS]
    aligned: dict[int, list[tuple[str, float, float]]] = {}
    if qwen_chunks:
        aligned.update(align_qwen(cfg, audio, qwen_chunks))
    if ctc_chunks:
        aligned.update(align_ctc(cfg, audio, ctc_chunks))

    words: list[Word] = []
    for c in chunks:
        stamps = aligned.get(id(c))
        if stamps:
            words.extend(attach_times(tokenize(c.text), stamps, (c.start, c.end)))
        else:
            words.extend(interpolate_words(c.text, speech, (c.start, c.end)))
    misses = sum(not w.aligned for w in words)
    if words and misses / len(words) > 0.1:
        log.warning("%.0f%% of words have interpolated timings", 100 * misses / len(words))

    return Transcript(
        source=str(video),
        duration=info.duration,
        language=dominant,
        words=words,
        speech=[(round(a, 3), round(b, 3)) for a, b in speech],
        chunks=[{"start": round(c.start, 3), "end": round(c.end, 3), "language": c.language, "text": c.text} for c in chunks],
    )
