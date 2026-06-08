#!/usr/bin/env python3
"""
Batch subtitle extractor for local video files.

Pipeline:
1) Try extracting embedded subtitle streams with ffmpeg.
2) If no soft subtitles exist, sample frames, crop subtitle ROI, OCR, merge duplicates.
3) Export .srt, .vtt and .txt.

Install:
  - Install ffmpeg first and make sure `ffmpeg` and `ffprobe` are on PATH.
  - Install Python deps. See README.md.

Example:
  python extract_subtitles.py "./videos/*.mp4" --out ./subs --interval 0.5 --roi 0.55 0.88
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence, Tuple

import cv2
import numpy as np


DEFAULT_BANNED_WORDS = [
    "免责声明",
    "本剧",
    "剧情纯属虚构",
    "请勿模仿",
    "未成年人",
    "版权",
    "著作权",
    "ICP备案",
    "技术支持",
    "短剧",
    "ShortTV",
    "ShortMax",
    "www.",
    "http",
]


@dataclass
class SubtitleCue:
    start: float
    end: float
    text: str


@dataclass
class OCRSample:
    t: float
    text: str
    score: float


def run(cmd: Sequence[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(cmd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def ensure_tools() -> None:
    missing = [tool for tool in ["ffmpeg", "ffprobe"] if shutil.which(tool) is None]
    if missing:
        raise RuntimeError(f"Missing required command(s): {', '.join(missing)}. Please install ffmpeg.")


def ffprobe_streams(video: Path) -> dict[str, Any]:
    proc = run([
        "ffprobe",
        "-v", "error",
        "-show_streams",
        "-of", "json",
        str(video),
    ])
    return json.loads(proc.stdout or "{}")


def has_subtitle_stream(video: Path) -> bool:
    try:
        data = ffprobe_streams(video)
    except Exception:
        return False
    return any(s.get("codec_type") == "subtitle" for s in data.get("streams", []))


def try_extract_soft_subtitle(video: Path, out_srt: Path) -> bool:
    """Try to extract the first embedded subtitle stream into SRT.

    Returns True if ffmpeg produced a non-empty SRT, False otherwise.
    """
    if not has_subtitle_stream(video):
        return False
    out_srt.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video),
        "-map", "0:s:0",
        "-c:s", "srt",
        str(out_srt),
    ]
    proc = run(cmd, check=False)
    return proc.returncode == 0 and out_srt.exists() and out_srt.stat().st_size > 0


def format_srt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def format_vtt_time(seconds: float) -> str:
    return format_srt_time(seconds).replace(",", ".")


def write_srt(cues: Sequence[SubtitleCue], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for i, cue in enumerate(cues, 1):
            f.write(f"{i}\n")
            f.write(f"{format_srt_time(cue.start)} --> {format_srt_time(cue.end)}\n")
            f.write(cue.text.strip() + "\n\n")


def write_vtt(cues: Sequence[SubtitleCue], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write("WEBVTT\n\n")
        for cue in cues:
            f.write(f"{format_vtt_time(cue.start)} --> {format_vtt_time(cue.end)}\n")
            f.write(cue.text.strip() + "\n\n")


def write_txt(cues: Sequence[SubtitleCue], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for cue in cues:
            f.write(cue.text.strip() + "\n")


def read_srt_text(path: Path) -> list[SubtitleCue]:
    """Tiny SRT reader used only for mirroring soft-sub output to TXT/VTT."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    blocks = re.split(r"\n\s*\n", text.strip())
    cues: list[SubtitleCue] = []
    for block in blocks:
        lines = [ln.strip("\ufeff") for ln in block.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        if re.match(r"^\d+$", lines[0]):
            lines = lines[1:]
        if not lines or "-->" not in lines[0]:
            continue
        start_s, end_s = [x.strip() for x in lines[0].split("-->", 1)]
        cues.append(SubtitleCue(parse_srt_time(start_s), parse_srt_time(end_s), "\n".join(lines[1:])))
    return cues


def parse_srt_time(value: str) -> float:
    value = value.replace(",", ".")
    m = re.search(r"(\d+):(\d+):(\d+)(?:\.(\d+))?", value)
    if not m:
        return 0.0
    h, mi, s, ms = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(s) + int((ms or "0").ljust(3, "0")[:3]) / 1000


def init_paddleocr(lang: str = "ch") -> Any:
    """Initialize PaddleOCR.

    The code supports both PaddleOCR 3.x `predict()` and older `ocr()` APIs.
    """
    from paddleocr import PaddleOCR  # Imported lazily so soft-sub extraction works without OCR deps.

    # PaddleOCR 3.x style. lang is supported by the OCR pipeline.
    try:
        return PaddleOCR(
            lang=lang,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            engine="paddle",
        )
    except TypeError:
        # PaddleOCR 2.x compatibility.
        return PaddleOCR(use_angle_cls=False, lang=lang, show_log=False)


def ocr_text_from_image(ocr: Any, image_path: Path, min_score: float = 0.35) -> Tuple[str, float]:
    """Return merged OCR text and average confidence from one image."""
    texts: list[str] = []
    scores: list[float] = []

    # PaddleOCR 3.x
    if hasattr(ocr, "predict"):
        try:
            results = ocr.predict(str(image_path))
            for res in results:
                data = None
                if isinstance(res, dict):
                    data = res
                elif hasattr(res, "json"):
                    data = res.json
                    if callable(data):
                        data = data()
                elif hasattr(res, "to_dict"):
                    data = res.to_dict()
                if not isinstance(data, dict):
                    continue
                payload = data.get("res", data)
                rec_texts = payload.get("rec_texts") or []
                rec_scores = payload.get("rec_scores") or []
                if isinstance(rec_scores, np.ndarray):
                    rec_scores = rec_scores.tolist()
                for i, txt in enumerate(rec_texts):
                    score = float(rec_scores[i]) if i < len(rec_scores) else 1.0
                    if score >= min_score:
                        texts.append(str(txt))
                        scores.append(score)
                rec_text = payload.get("rec_text")
                if rec_text:
                    score = float(payload.get("rec_score", 1.0))
                    if score >= min_score:
                        texts.append(str(rec_text))
                        scores.append(score)
        except Exception:
            pass

    # PaddleOCR 2.x fallback.
    if not texts and hasattr(ocr, "ocr"):
        try:
            result = ocr.ocr(str(image_path), cls=False)
            # Typical shape: [ [ [box, (text, score)], ... ] ]
            for page in result or []:
                for item in page or []:
                    if len(item) < 2:
                        continue
                    txt, score = item[1][0], float(item[1][1])
                    if score >= min_score:
                        texts.append(str(txt))
                        scores.append(score)
        except Exception:
            pass

    text = normalize_text("".join(texts))
    score = float(np.mean(scores)) if scores else 0.0
    return text, score


def normalize_text(text: str) -> str:
    text = text.strip()
    text = re.sub(r"[\s\u3000]+", "", text)
    # Normalize common OCR punctuation noise. Keep Chinese punctuation when useful.
    text = text.replace("|", "").replace("丨", "")
    text = text.replace("，", "，").replace(",", "，")
    # Remove repeated punctuation.
    text = re.sub(r"[·•。.,，]{2,}", "。", text)
    return text.strip("-_—~·•。.,， ")


def looks_like_dialogue(text: str, banned_words: Sequence[str], min_chars: int = 2) -> bool:
    if len(text) < min_chars:
        return False
    if not re.search(r"[\u4e00-\u9fff]", text):
        return False
    if any(word and word in text for word in banned_words):
        return False
    # Too many digits/latin chars is usually watermark or URL.
    latin_digit = len(re.findall(r"[A-Za-z0-9]", text))
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    if latin_digit > max(6, cjk):
        return False
    return True


def crop_subtitle_roi(frame: np.ndarray, y1_ratio: float, y2_ratio: float, x_margin_ratio: float) -> np.ndarray:
    h, w = frame.shape[:2]
    y1 = max(0, min(h - 1, int(h * y1_ratio)))
    y2 = max(y1 + 1, min(h, int(h * y2_ratio)))
    x1 = max(0, min(w - 1, int(w * x_margin_ratio)))
    x2 = max(x1 + 1, min(w, int(w * (1.0 - x_margin_ratio))))
    return frame[y1:y2, x1:x2]


def preprocess_for_ocr(roi: np.ndarray, scale: float = 2.0) -> np.ndarray:
    """Enhance the cropped subtitle area before OCR.

    For white subtitles with dark outline, a contrast boost plus resizing is usually enough.
    """
    if scale != 1.0:
        roi = cv2.resize(roi, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    # Mild denoise and contrast boost.
    lab = cv2.cvtColor(roi, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    l2 = clahe.apply(l)
    enhanced = cv2.merge([l2, a, b])
    return cv2.cvtColor(enhanced, cv2.COLOR_LAB2BGR)


def sample_video_ocr(
    video: Path,
    ocr: Any,
    work_dir: Path,
    interval: float,
    roi: Tuple[float, float],
    x_margin: float,
    banned_words: Sequence[str],
    min_score: float,
    min_chars: int,
    max_frames: Optional[int] = None,
    debug: bool = False,
) -> list[OCRSample]:
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = frame_count / fps if frame_count else 0.0
    if duration <= 0:
        duration = 60 * 60  # fallback; loop stops when frames end

    frames_dir = work_dir / "ocr_frames"
    if debug:
        frames_dir.mkdir(parents=True, exist_ok=True)

    samples: list[OCRSample] = []
    t = 0.0
    n = 0
    while t <= duration:
        if max_frames is not None and n >= max_frames:
            break
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, frame = cap.read()
        if not ok:
            break
        crop = crop_subtitle_roi(frame, roi[0], roi[1], x_margin)
        prep = preprocess_for_ocr(crop)

        if debug:
            image_path = frames_dir / f"frame_{n:06d}_{t:.2f}.png"
        else:
            image_path = work_dir / "_tmp_ocr.png"
        cv2.imwrite(str(image_path), prep)

        text, score = ocr_text_from_image(ocr, image_path, min_score=min_score)
        if looks_like_dialogue(text, banned_words, min_chars=min_chars):
            samples.append(OCRSample(t=t, text=text, score=score))
        else:
            samples.append(OCRSample(t=t, text="", score=score))

        t += interval
        n += 1

    cap.release()
    return samples


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def merge_samples_to_cues(
    samples: Sequence[OCRSample],
    interval: float,
    sim_threshold: float,
    gap_tolerance: float,
    min_duration: float,
    max_duration: float,
) -> list[SubtitleCue]:
    cues: list[SubtitleCue] = []
    cur_text = ""
    cur_start = 0.0
    cur_last = 0.0
    cur_score = 0.0

    def close_current(end_hint: Optional[float] = None) -> None:
        nonlocal cur_text, cur_start, cur_last, cur_score
        if not cur_text:
            return
        end = end_hint if end_hint is not None else cur_last + interval
        end = max(end, cur_start + min_duration)
        if end - cur_start > max_duration:
            end = cur_start + max_duration
        cues.append(SubtitleCue(start=cur_start, end=end, text=cur_text))
        cur_text = ""
        cur_score = 0.0

    for smp in samples:
        text = smp.text
        t = smp.t
        if not text:
            if cur_text and (t - cur_last) > gap_tolerance:
                close_current(cur_last + interval)
            continue

        if not cur_text:
            cur_text = text
            cur_start = t
            cur_last = t
            cur_score = smp.score
            continue

        same = similarity(cur_text, text) >= sim_threshold
        if same or (text in cur_text) or (cur_text in text):
            # Keep the most complete/high-confidence variant of the same subtitle.
            if len(text) > len(cur_text) or smp.score > cur_score + 0.08:
                cur_text = text
                cur_score = smp.score
            cur_last = t
        else:
            close_current(min(t, cur_last + interval))
            cur_text = text
            cur_start = t
            cur_last = t
            cur_score = smp.score

    close_current()
    return postprocess_cues(cues)


def postprocess_cues(cues: Sequence[SubtitleCue]) -> list[SubtitleCue]:
    """Final cleanup: remove exact duplicate adjacent cues and fix overlaps."""
    out: list[SubtitleCue] = []
    for cue in cues:
        text = normalize_text(cue.text)
        if not text:
            continue
        if out and out[-1].text == text and cue.start - out[-1].end <= 1.0:
            out[-1].end = max(out[-1].end, cue.end)
            continue
        start = cue.start
        end = max(cue.end, start + 0.3)
        if out and start < out[-1].end:
            start = out[-1].end
            end = max(end, start + 0.3)
        out.append(SubtitleCue(start=start, end=end, text=text))
    return out


def process_video(args: argparse.Namespace, video: Path, ocr: Optional[Any]) -> None:
    safe_stem = video.stem.replace(os.sep, "_")
    out_dir = Path(args.out)
    out_srt = out_dir / f"{safe_stem}_提取字幕.srt"
    out_vtt = out_dir / f"{safe_stem}_提取字幕.vtt"
    out_txt = out_dir / f"{safe_stem}_字幕文本.txt"
    out_dir.mkdir(parents=True, exist_ok=True)

    if not args.force_ocr:
        if try_extract_soft_subtitle(video, out_srt):
            cues = read_srt_text(out_srt)
            write_vtt(cues, out_vtt)
            write_txt(cues, out_txt)
            print(f"[soft-sub] {video.name} -> {out_srt.name}")
            return

    if ocr is None:
        ocr = init_paddleocr(args.lang)

    with tempfile.TemporaryDirectory(prefix="sub_ocr_") as tmp:
        work_dir = Path(tmp)
        samples = sample_video_ocr(
            video=video,
            ocr=ocr,
            work_dir=work_dir,
            interval=args.interval,
            roi=(args.roi[0], args.roi[1]),
            x_margin=args.x_margin,
            banned_words=args.ban,
            min_score=args.min_score,
            min_chars=args.min_chars,
            max_frames=args.max_frames,
            debug=args.debug,
        )
        cues = merge_samples_to_cues(
            samples,
            interval=args.interval,
            sim_threshold=args.sim_threshold,
            gap_tolerance=args.gap_tolerance,
            min_duration=args.min_duration,
            max_duration=args.max_duration,
        )

    write_srt(cues, out_srt)
    write_vtt(cues, out_vtt)
    write_txt(cues, out_txt)
    print(f"[ocr] {video.name} -> {len(cues)} cues")


def expand_inputs(patterns: Sequence[str]) -> list[Path]:
    files: list[Path] = []
    for pattern in patterns:
        matches = glob.glob(pattern)
        if matches:
            files.extend(Path(m) for m in matches)
        else:
            files.append(Path(pattern))
    return [p for p in files if p.exists() and p.is_file()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Extract soft or hard subtitles from local videos.")
    p.add_argument("inputs", nargs="+", help="Video paths or glob patterns, e.g. './videos/*.mp4'")
    p.add_argument("--out", default="./subtitles", help="Output directory")
    p.add_argument("--force-ocr", action="store_true", help="Skip soft subtitle extraction and force OCR")
    p.add_argument("--interval", type=float, default=0.5, help="Frame sampling interval in seconds")
    p.add_argument("--roi", nargs=2, type=float, default=[0.55, 0.88], metavar=("Y1", "Y2"), help="Subtitle crop vertical range as ratios, default: 0.55 0.88")
    p.add_argument("--x-margin", type=float, default=0.04, help="Left/right crop margin ratio")
    p.add_argument("--lang", default="ch", help="PaddleOCR language code, default: ch")
    p.add_argument("--min-score", type=float, default=0.35, help="Minimum OCR confidence")
    p.add_argument("--min-chars", type=int, default=2, help="Minimum number of recognized chars")
    p.add_argument("--sim-threshold", type=float, default=0.86, help="Similarity threshold for merging duplicate samples")
    p.add_argument("--gap-tolerance", type=float, default=1.2, help="Allowed OCR-missing gap before closing a cue")
    p.add_argument("--min-duration", type=float, default=0.6, help="Minimum cue duration")
    p.add_argument("--max-duration", type=float, default=6.0, help="Maximum cue duration")
    p.add_argument("--ban", nargs="*", default=DEFAULT_BANNED_WORDS, help="Words to exclude, useful for watermarks/disclaimers")
    p.add_argument("--max-frames", type=int, default=None, help="Debug limit: OCR only first N sampled frames")
    p.add_argument("--debug", action="store_true", help="Keep OCR frame crops in a temp folder while running")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    ensure_tools()
    files = expand_inputs(args.inputs)
    if not files:
        print("No input videos found.", file=sys.stderr)
        return 2

    # Reuse one OCR model across videos for speed. Soft-sub extraction still happens first per video.
    ocr = None
    if args.force_ocr or any(not has_subtitle_stream(f) for f in files):
        ocr = init_paddleocr(args.lang)

    for video in files:
        process_video(args, video, ocr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
