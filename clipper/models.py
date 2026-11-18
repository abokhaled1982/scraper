from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Word:
    text: str
    start: float
    end: float
    aligned: bool = True  # False when timing was interpolated (aligner miss)


@dataclass
class Transcript:
    source: str
    duration: float
    language: str | None
    words: list[Word]
    speech: list[tuple[float, float]] = field(default_factory=list)  # VAD speech regions in seconds
    chunks: list[dict] = field(default_factory=list)  # {"start","end","language","text"}

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(asdict(self), ensure_ascii=False, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "Transcript":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        data["words"] = [Word(**w) for w in data["words"]]
        data["speech"] = [tuple(s) for s in data.get("speech", [])]
        return cls(**data)


@dataclass
class Sentence:
    id: int
    start: float
    end: float
    first_word: int
    last_word: int  # inclusive
    text: str


@dataclass
class Moment:
    start: float
    end: float
    first_word: int
    last_word: int
    score: float
    title: str = ""
    hook: str = ""
    reason: str = ""
    source: str = "heuristic"  # heuristic | llm
    llm_score: float | None = None
    heuristic_score: float | None = None

    @property
    def duration(self) -> float:
        return self.end - self.start


def save_moments(moments: list[Moment], path: str | Path) -> None:
    Path(path).write_text(json.dumps([asdict(m) for m in moments], ensure_ascii=False, indent=1), encoding="utf-8")


def load_moments(path: str | Path) -> list[Moment]:
    return [Moment(**m) for m in json.loads(Path(path).read_text(encoding="utf-8"))]
