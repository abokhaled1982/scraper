"""Language-aware heuristic 'virality' signals computed from the ASR data and the audio envelope.

Used on its own when no LLM is available and blended with the LLM score otherwise.
"""

from __future__ import annotations

import math
import re

import numpy as np

from .models import Sentence, Transcript
from .segment import SENTENCE_END, ends_with
from .textalign import normalize

QUESTION_MARKS = ("?", "؟")

LEXICON: dict[str, dict[str, set[str]]] = {
    "en": {
        "question": {"why", "how", "what", "who", "when", "where", "which"},
        "hook": {
            "secret", "secrets", "mistake", "mistakes", "never", "always", "truth", "crazy", "insane",
            "unbelievable", "worst", "best", "money", "rich", "broke", "tip", "warning", "imagine",
            "honestly", "nobody", "everyone", "actually", "shocking", "biggest", "hate", "love", "stop",
            "wrong", "problem", "million", "fired", "died", "lie", "lied", "realized", "changed",
        },
        "weak_start": {"and", "but", "so", "because", "um", "uh", "like", "or", "then", "yeah", "also", "okay"},
        "dangling": {"he", "she", "it", "that", "this", "they", "those", "these", "him", "her", "them"},
    },
    "de": {
        "question": {"warum", "wieso", "weshalb", "wie", "was", "wer", "wann", "wo", "welche", "welcher"},
        "hook": {
            "geheimnis", "fehler", "niemals", "nie", "immer", "wahrheit", "krass", "wahnsinn", "unglaublich",
            "schlimmste", "beste", "geld", "reich", "pleite", "tipp", "achtung", "stell", "ehrlich", "niemand",
            "jeder", "eigentlich", "schockierend", "größte", "hasse", "liebe", "stopp", "falsch", "problem",
            "million", "gekündigt", "lüge", "gelogen", "verstanden", "verändert",
        },
        "weak_start": {"und", "aber", "also", "dann", "ähm", "äh", "ja", "genau", "oder", "weil", "sondern", "naja", "okay"},
        "dangling": {"er", "sie", "es", "das", "dies", "diese", "dieser", "dem", "den", "ihm", "ihr", "ihn"},
    },
    "ar": {
        "question": {"لماذا", "ليش", "ليه", "كيف", "شلون", "هل", "متى", "وين", "اين", "ايش", "شو", "ماذا", "مين", "من"},
        "hook": {
            "سر", "اسرار", "خطا", "اخطاء", "ابدا", "مستحيل", "صدمه", "حقيقه", "الحقيقه", "اهم", "افضل",
            "اسوا", "فلوس", "مال", "نصيحه", "انتبه", "تخيل", "والله", "صراحه", "بصراحه", "مجنون", "اول",
            "مليون", "غلط", "مشكله", "كذب", "اكتشفت", "تغيرت", "احذر", "خطير", "عجيب", "غريب",
        },
        "weak_start": {"و", "بس", "يعني", "ف", "طيب", "اه", "ثم", "لكن", "او", "اوكي", "ايه"},
        "dangling": {"هو", "هي", "هذا", "هذه", "ذلك", "تلك", "هم", "هذي", "هاد", "هادا"},
    },
}

_NUMBER = re.compile(r"\d")


class SignalModel:
    def __init__(self, transcript: Transcript, sentences: list[Sentence], env_db: np.ndarray, hop: float = 0.01):
        self.t = transcript
        self.env = env_db
        self.hop = hop
        lex_lang = transcript.language if transcript.language in LEXICON else "en"
        self.lex = LEXICON[lex_lang]

        mask = np.zeros(len(env_db), dtype=bool)
        for a, b in transcript.speech:
            mask[int(a / hop) : int(b / hop)] = True
        speech_db = env_db[mask] if mask.any() else env_db
        self.mu = float(np.mean(speech_db))
        self.sd = float(np.std(speech_db)) or 1.0

        rates = [self._rate(s) for s in sentences] or [2.5]
        self.rate_mu = float(np.mean(rates))
        self.rate_sd = float(np.std(rates)) or 1.0
    def _rate(self, s: Sentence) -> float:
        return (s.last_word - s.first_word + 1) / max(0.3, s.end - s.start)

    def _db(self, start: float, end: float) -> np.ndarray:
        seg = self.env[int(start / self.hop) : max(int(start / self.hop) + 1, int(end / self.hop))]
        return seg if len(seg) else np.array([self.mu])

    def _tokens(self, text: str) -> list[str]:
        return [normalize(t) for t in text.split() if normalize(t)]

    def hook_score(self, s: Sentence) -> float:
        toks = self._tokens(s.text)
        if not toks:
            return 0.0
        score = 0.0
        if ends_with(s.text, QUESTION_MARKS) or toks[0] in self.lex["question"]:
            score += 0.35
        score += min(0.5, 0.25 * sum(t in self.lex["hook"] for t in toks[:15]))
        if _NUMBER.search(s.text):
            score += 0.15
        if ends_with(s.text, ("!",)):
            score += 0.15
        if toks[0] in self.lex["weak_start"]:
            score -= 0.4
        if toks[0] in self.lex["dangling"]:
            score -= 0.2
        return float(np.clip(score, 0.0, 1.0))

    def window_score(self, sentences: list[Sentence], target_sec: float) -> tuple[float, dict]:
        first, last = sentences[0], sentences[-1]
        start, end = first.start, last.end
        dur = max(0.1, end - start)
        db = self._db(start, end)
        loud = float(np.clip((np.mean(db) - self.mu) / self.sd, -2, 2))
        peak = float(np.clip((np.percentile(db, 95) - self.mu) / self.sd, -2, 3))
        rate = float(np.clip((np.mean([self._rate(s) for s in sentences]) - self.rate_mu) / self.rate_sd, -2, 2))
        spoken = sum(s.end - s.start for s in sentences)
        density = float(np.clip(spoken / dur, 0, 1))
        toks = [t for s in sentences for t in self._tokens(s.text)]
        emotion = min(1.0, 8.0 * sum(t in self.lex["hook"] for t in toks) / max(1, len(toks)))
        ending = 1.0 if ends_with(last.text, SENTENCE_END) else 0.4
        length = math.exp(-(((dur - target_sec) / (0.6 * target_sec)) ** 2))

        parts = {
            "hook": self.hook_score(first),
            "energy": (loud + 2) / 4,
            "peak": (peak + 2) / 5,
            "pace": (rate + 2) / 4,
            "density": density,
            "emotion": emotion,
            "ending": ending,
            "length": length,
        }
        weights = {"hook": 0.24, "energy": 0.16, "peak": 0.10, "pace": 0.06, "density": 0.12, "emotion": 0.12, "ending": 0.10, "length": 0.10}
        return 100.0 * sum(weights[k] * parts[k] for k in weights), parts
