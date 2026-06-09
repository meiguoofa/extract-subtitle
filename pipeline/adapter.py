"""Convert vendor-agnostic ASR results into subtitle cues."""
from __future__ import annotations

from .models import ASRResult, ASRUtterance, ASRWord, SubtitleCue


def asr_result_to_cues(
    result: ASRResult,
    max_chars_per_cue: int = 30,
    max_duration_sec: float = 6.0,
    min_gap_merge_sec: float = 0.3,
    min_merge_chars: int = 6,
) -> list[SubtitleCue]:
    cues: list[SubtitleCue] = []
    for utt in result.utterances:
        if not utt.text.strip():
            continue
        cues.extend(_split_utterance(utt, max_chars_per_cue, max_duration_sec))

    if not cues:
        return cues
    return _merge_short_adjacent(cues, min_gap_merge_sec, min_merge_chars, max_chars_per_cue, max_duration_sec)


def _split_utterance(
    utt: ASRUtterance,
    max_chars: int,
    max_duration_sec: float,
) -> list[SubtitleCue]:
    duration_sec = (utt.end_ms - utt.start_ms) / 1000.0
    if len(utt.text) <= max_chars and duration_sec <= max_duration_sec:
        return [SubtitleCue(start=utt.start_ms / 1000.0, end=utt.end_ms / 1000.0, text=utt.text)]

    if utt.words:
        return _split_by_words(utt.words, max_chars, max_duration_sec)

    chunks: list[str] = []
    text = utt.text
    while text:
        chunks.append(text[:max_chars])
        text = text[max_chars:]
    if not chunks:
        return []
    span = (utt.end_ms - utt.start_ms) / len(chunks) / 1000.0
    out = []
    for i, chunk in enumerate(chunks):
        s = utt.start_ms / 1000.0 + i * span
        e = s + span
        out.append(SubtitleCue(start=s, end=e, text=chunk))
    return out


def _split_by_words(
    words: list[ASRWord],
    max_chars: int,
    max_duration_sec: float,
) -> list[SubtitleCue]:
    out: list[SubtitleCue] = []
    cur: list[ASRWord] = []
    cur_chars = 0
    cur_start = words[0].start_ms

    def flush(end_ms: int) -> None:
        nonlocal cur, cur_chars
        if not cur:
            return
        text = "".join(w.text for w in cur)
        out.append(SubtitleCue(start=cur_start / 1000.0, end=end_ms / 1000.0, text=text))
        cur = []
        cur_chars = 0

    for w in words:
        if not cur:
            cur_start = w.start_ms
        prospective_chars = cur_chars + len(w.text)
        prospective_dur = (w.end_ms - cur_start) / 1000.0
        if cur and (prospective_chars > max_chars or prospective_dur > max_duration_sec):
            flush(cur[-1].end_ms)
            cur_start = w.start_ms
        cur.append(w)
        cur_chars = sum(len(x.text) for x in cur)

    if cur:
        flush(cur[-1].end_ms)
    return out


def _merge_short_adjacent(
    cues: list[SubtitleCue],
    min_gap_sec: float,
    min_merge_chars: int,
    max_chars_per_cue: int,
    max_duration_sec: float,
) -> list[SubtitleCue]:
    out = [cues[0]]
    for cue in cues[1:]:
        prev = out[-1]
        gap = cue.start - prev.end
        merged_chars = len(prev.text) + len(cue.text)
        merged_dur = cue.end - prev.start
        if (
            gap <= min_gap_sec
            and (len(prev.text) < min_merge_chars or len(cue.text) < min_merge_chars)
            and merged_chars <= max_chars_per_cue
            and merged_dur <= max_duration_sec
        ):
            out[-1] = SubtitleCue(start=prev.start, end=cue.end, text=prev.text + cue.text)
        else:
            out.append(cue)
    return out
