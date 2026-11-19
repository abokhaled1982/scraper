"""Clients for the remote GPU workers.

- ASR worker (colab_asr_server.ipynb): audio upload -> job -> word-timestamped transcript.
- LLM worker (llama-server, e.g. Gemma 4 on Kaggle): OpenAI chat completions, streamed so that
  Cloudflare quick tunnels (100 s idle limit) never cut a long answer.
"""

from __future__ import annotations

import difflib
import http.client
import json
import logging
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .config import LANGUAGES, ClipperConfig
from .media import run
from .models import Transcript, Word
from .segment import split_sentences
from .textalign import attach_times, normalize, tokenize

log = logging.getLogger("clipper.remote")

# Cloudflare blocks urllib's default "Python-urllib" user agent.
USER_AGENT = "clipper-orchestrator/1.0"
_NETWORK_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException)


def _base(url: str) -> str:
    url = url.strip().rstrip("/")
    for suffix in ("/v1/chat/completions", "/v1"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return url


def _open(url: str, token: str, data: bytes | None = None, content_type: str = "application/json", timeout: float = 60):
    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        headers["Content-Type"] = content_type
    return urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=timeout)


def _call(url: str, token: str, data: bytes | None = None, content_type: str = "application/json",
          timeout: float = 60, retries: int = 4) -> dict:
    for attempt in range(retries):
        try:
            with _open(url, token, data, content_type, timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code < 500 or attempt == retries - 1:
                raise RuntimeError(f"{url}: HTTP {exc.code} {exc.read()[:300].decode(errors='replace')}") from exc
        except _NETWORK_ERRORS as exc:
            if attempt == retries - 1:
                raise RuntimeError(f"{url} not reachable: {exc}") from exc
        time.sleep(2 * 2**attempt)
    raise AssertionError("unreachable")


def check_workers(cfg: ClipperConfig) -> None:
    """Fail fast before minutes of work if a configured worker is down or the token is wrong."""
    if cfg.asr_url:
        _call(f"{_base(cfg.asr_url)}/health", cfg.asr_token, retries=2)
        _call(f"{_base(cfg.asr_url)}/auth", cfg.asr_token, retries=2)
        log.info("ASR worker OK: %s", _base(cfg.asr_url))
    if cfg.llm_url:
        _call(f"{_base(cfg.llm_url)}/health", cfg.llm_token, retries=2)
        # /health and /v1/models are public in llama-server; /tokenize needs the API key.
        _call(f"{_base(cfg.llm_url)}/tokenize", cfg.llm_token, json.dumps({"content": "ok"}).encode(), retries=2)
        log.info("LLM worker OK: %s", _base(cfg.llm_url))


# ---------------------------------------------------------------- ASR worker

def transcribe_remote(video: str, cfg: ClipperConfig, poll_sec: float = 5.0) -> Transcript:
    base = _base(cfg.asr_url)
    with tempfile.TemporaryDirectory() as tmp:
        audio = Path(tmp) / "audio.ogg"
        # Opus 48 kb/s mono 16 kHz is transparent for speech and keeps uploads small (~22 MB per hour).
        run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", video, "-vn", "-ac", "1", "-ar", "16000",
             "-c:a", "libopus", "-b:a", "48k", str(audio)])
        log.info("Uploading %.1f MB audio to the ASR worker", audio.stat().st_size / 1e6)
        query = urllib.parse.urlencode({"language": cfg.language, "context": cfg.asr_context})
        job = _call(f"{base}/jobs?{query}", cfg.asr_token, audio.read_bytes(), "application/octet-stream", timeout=600)

    started, last_stage = time.time(), None
    while True:
        time.sleep(poll_sec)
        status = _call(f"{base}/jobs/{job['id']}", cfg.asr_token, timeout=120)
        if status["status"] == "done":
            break
        if status["status"] == "error":
            raise RuntimeError(f"ASR worker failed: {status.get('error')}")
        stage = status.get("stage") or status["status"]
        if stage != last_stage:
            log.info("ASR worker [%3.0fs]: %s", time.time() - started, stage)
            last_stage = stage

    data = status["result"]
    data["source"] = str(video)
    data["words"] = [Word(**w) for w in data["words"]]
    data["speech"] = [tuple(s) for s in data.get("speech", [])]
    return Transcript(**data)


# ---------------------------------------------------------------- LLM worker

class RemoteLLM:
    """Same interface as scorer.LocalLLM, backed by an OpenAI-compatible llama-server."""

    def __init__(self, cfg: ClipperConfig):
        self.url = f"{_base(cfg.llm_url)}/v1/chat/completions"
        self.token = cfg.llm_token
        self.model = cfg.llm_api_model
        self.max_tokens = cfg.llm_max_tokens

    def chat(self, system: str, user: str, schema: dict | None = None) -> str:
        payload: dict = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "stream": True,
        }
        if schema is not None:
            payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "clipper", "strict": True, "schema": schema}}
        body = json.dumps(payload).encode()
        for attempt in range(3):
            try:
                return self._stream(body)
            except urllib.error.HTTPError as exc:
                detail = exc.read()[:500].decode(errors="replace")
                if exc.code < 500 or attempt == 2:
                    raise RuntimeError(f"LLM worker HTTP {exc.code}: {detail}") from exc
            except _NETWORK_ERRORS as exc:
                if attempt == 2:
                    raise RuntimeError(f"LLM worker not reachable: {exc}") from exc
                log.warning("LLM request failed (%s); retrying", exc)
            time.sleep(5 * (attempt + 1))
        raise AssertionError("unreachable")

    def _stream(self, body: bytes) -> str:
        parts: list[str] = []
        finish = None
        with _open(self.url, self.token, body, timeout=900) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if "error" in chunk:
                    raise RuntimeError(f"LLM worker error: {chunk['error']}")
                choice = (chunk.get("choices") or [{}])[0]
                parts.append((choice.get("delta") or {}).get("content") or "")
                finish = choice.get("finish_reason") or finish
        if finish == "length":
            log.warning("LLM answer hit max_tokens=%d and may be truncated", self.max_tokens)
        return "".join(parts)

    def close(self) -> None:
        pass


# ---------------------------------------------------------------- transcript correction

CORRECT_SYSTEM = (
    "You are a meticulous transcript editor. You fix automatic speech recognition errors without changing "
    "what the speaker said. You answer only with valid JSON."
)

CORRECT_PROMPT = """Automatic speech recognition transcript (language: {language}). Each line: [id] text
{glossary}
{lines}

Fix only clear recognition errors: misheard or misspelled words, wrong names and technical terms, wrongly split or joined words, missing punctuation and sentence-final marks.
Rules:
- Keep the speaker's exact wording, dialect, word order and length. Never paraphrase, translate, summarise, censor or add content.
- Never merge, split or reorder lines.
- Return only the lines you changed, each with its id and full corrected text. Return an empty list if nothing needs fixing."""

CORRECT_SCHEMA = {
    "type": "object",
    "properties": {
        "fixes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "text": {"type": "string"}},
                "required": ["id", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["fixes"],
    "additionalProperties": False,
}


def _plausible_fix(old: list[str], new: list[str], min_similarity: float = 0.6) -> bool:
    """Reject rewrites: a correction keeps roughly the same words, so timings can be carried over."""
    if not new or abs(len(new) - len(old)) > max(2, 0.25 * len(old)):
        return False
    a = " ".join(normalize(t) for t in old)
    b = " ".join(normalize(t) for t in new)
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() >= min_similarity


def correct_transcript(transcript: Transcript, cfg: ClipperConfig) -> tuple[Transcript, list[dict]]:
    """Let the remote LLM fix ASR errors per sentence; new words inherit the original word timings."""
    sentences = split_sentences(transcript.words)
    if not sentences:
        return transcript, []
    llm = RemoteLLM(cfg)
    language = LANGUAGES.get(transcript.language or "", ("the transcript's language",))[0]
    glossary = f"Correct spelling of names/terms: {cfg.asr_context}\n" if cfg.asr_context else ""

    batches, current, count = [], [], 0
    for s in sentences:
        n = s.last_word - s.first_word + 1
        if current and count + n > cfg.llm_correct_words:
            batches.append(current)
            current, count = [], 0
        current.append(s)
        count += n
    if current:
        batches.append(current)

    fixes: dict[int, str] = {}
    for n, batch in enumerate(batches, 1):
        lines = "\n".join(f"[{s.id}] {s.text}" for s in batch)
        ids = {s.id for s in batch}
        try:
            raw = llm.chat(CORRECT_SYSTEM, CORRECT_PROMPT.format(language=language, glossary=glossary, lines=lines), CORRECT_SCHEMA)
            items = json.loads(raw).get("fixes", [])
        except Exception as exc:  # keep the raw ASR text for this batch
            log.warning("Correction batch %d/%d failed (%s); keeping ASR text", n, len(batches), exc)
            continue
        for it in items:
            if isinstance(it, dict) and it.get("id") in ids and isinstance(it.get("text"), str):
                fixes[it["id"]] = it["text"].strip()
        log.info("Correction %d/%d: %d lines changed", n, len(batches), sum(1 for i in ids if i in fixes))

    words: list[Word] = []
    changes: list[dict] = []
    rejected = 0
    for s in sentences:
        original = transcript.words[s.first_word : s.last_word + 1]
        new_tokens = tokenize(fixes.get(s.id, ""))
        old_tokens = [w.text for w in original]
        if s.id not in fixes or new_tokens == old_tokens:
            words.extend(original)
            continue
        if not _plausible_fix(old_tokens, new_tokens):
            rejected += 1
            words.extend(original)
            continue
        stamps = [(w.text, w.start, w.end) for w in original]
        words.extend(attach_times(new_tokens, stamps, (s.start, s.end)))
        changes.append({"id": s.id, "start": round(s.start, 2), "before": s.text, "after": " ".join(new_tokens)})
    log.info("Transcript correction: %d sentences fixed, %d implausible rewrites rejected", len(changes), rejected)

    corrected = Transcript(source=transcript.source, duration=transcript.duration, language=transcript.language,
                           words=words, speech=transcript.speech, chunks=transcript.chunks)
    return corrected, changes
