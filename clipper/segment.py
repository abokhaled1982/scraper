from __future__ import annotations

from .models import Sentence, Word

SENTENCE_END = (".", "!", "?", "…", "؟", "。", "!")
CLAUSE_END = (",", "،", ";", "؛", ":", "—", "-")
_TRAILING = "\"'»”’)]}"


def ends_with(text: str, marks: tuple[str, ...]) -> bool:
    return text.rstrip(_TRAILING).endswith(marks)


def split_sentences(words: list[Word], max_gap: float = 0.9, soft_max_sec: float = 15.0, hard_max_words: int = 60) -> list[Sentence]:
    """Group words into sentence-like units using punctuation, pauses and length limits."""
    sentences: list[Sentence] = []
    begin = 0
    for i, w in enumerate(words):
        last = i == len(words) - 1
        gap = (words[i + 1].start - w.end) if not last else 0.0
        length = w.end - words[begin].start
        n_words = i - begin + 1
        brk = (
            last
            or ends_with(w.text, SENTENCE_END)
            or gap >= max_gap
            or (length >= soft_max_sec and (ends_with(w.text, CLAUSE_END) or gap >= 0.35))
            or n_words >= hard_max_words
        )
        if brk:
            sentences.append(
                Sentence(
                    id=len(sentences),
                    start=words[begin].start,
                    end=w.end,
                    first_word=begin,
                    last_word=i,
                    text=" ".join(x.text for x in words[begin : i + 1]),
                )
            )
            begin = i + 1
    return sentences


def candidate_windows(sentences: list[Sentence], min_sec: float, max_sec: float) -> list[tuple[int, int]]:
    """All (first_sentence, last_sentence) ranges whose duration lies in [min_sec, max_sec]."""
    out = []
    for i, first in enumerate(sentences):
        for j in range(i, len(sentences)):
            dur = sentences[j].end - first.start
            if dur > max_sec:
                break
            if dur >= min_sec:
                out.append((i, j))
    return out


def fit_range(sentences: list[Sentence], i: int, j: int, min_sec: float, max_sec: float) -> tuple[int, int]:
    """Clamp a sentence range into [min_sec, max_sec], keeping the opening (the hook) intact."""
    i = max(0, min(i, len(sentences) - 1))
    j = max(i, min(j, len(sentences) - 1))
    while j > i and sentences[j].end - sentences[i].start > max_sec:
        j -= 1
    while sentences[j].end - sentences[i].start < min_sec:
        if j + 1 < len(sentences) and sentences[j + 1].end - sentences[i].start <= max_sec:
            j += 1
        elif i > 0 and sentences[j].end - sentences[i - 1].start <= max_sec:
            i -= 1
        else:
            break
    return i, j


def trim_words_to(words: list[Word], first: int, last: int, max_sec: float) -> int:
    """If a single sentence range is still too long, end at the best clause/pause boundary before max_sec."""
    limit = words[first].start + max_sec
    if words[last].end <= limit:
        return last
    best, best_score = first, -1.0
    for k in range(first, last):
        if words[k].end > limit:
            break
        gap = words[k + 1].start - words[k].end
        score = gap + (1.0 if ends_with(words[k].text, SENTENCE_END) else 0.5 if ends_with(words[k].text, CLAUSE_END) else 0.0)
        score += 0.2 * (words[k].end - words[first].start) / max_sec  # prefer longer
        if score > best_score:
            best, best_score = k, score
    return best
