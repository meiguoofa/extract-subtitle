"""Unified data models for ASR results."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ASRWord:
    text: str
    start_ms: int
    end_ms: int


@dataclass
class ASRUtterance:
    text: str
    start_ms: int
    end_ms: int
    words: list[ASRWord] = field(default_factory=list)


@dataclass
class ASRResult:
    utterances: list[ASRUtterance]
    detected_lang: str
    duration_sec: float
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class SubtitleCue:
    """Subtitle cue shared between OCR and ASR pipelines."""
    start: float
    end: float
    text: str
