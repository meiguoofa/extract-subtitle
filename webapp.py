"""FastAPI web frontend for video subtitle extraction."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from pipeline.db import (
    init_db, insert_job, update_job_progress, complete_job, fail_job,
    mark_interrupted_jobs, list_jobs, get_job, get_job_files, count_jobs,
    cleanup_old_jobs,
)

# ---------------------------------------------------------------------------
# Job model (in-memory, for real-time SSE progress of running jobs)
# ---------------------------------------------------------------------------

JOBS_DIR = Path("/tmp/subtitle_jobs")
MAX_UPLOAD_BYTES = 500 * 1024 * 1024
JOB_TTL_SEC = 2 * 3600
DB_PATH = Path(__file__).parent / "data" / "subtitles.db"

FMT_MAP = {
    "srt": ("source", ".srt"),
    "vtt": ("source", ".vtt"),
    "txt": ("source", ".txt"),
    "translated_srt": ("translated", ".srt"),
    "translated_vtt": ("translated", ".vtt"),
    "translated_txt": ("translated", ".txt"),
}


@dataclass
class Job:
    job_id: str
    status: str = "queued"
    stage: str = ""
    message: str = ""
    percent: int = 0
    error: str | None = None
    files: dict = field(default_factory=dict)
    file_paths: dict = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    event_flag: threading.Event = field(default_factory=threading.Event)
    job_dir: Path = field(default_factory=Path)


jobs: dict[str, Job] = {}
_loop: asyncio.AbstractEventLoop | None = None
_db: aiosqlite.Connection | None = None


def _emit(job: Job, stage: str, message: str, percent: int,
          event_type: str = "progress", **extra) -> None:
    job.stage = stage
    job.message = message
    job.percent = percent
    data: dict = {"stage": stage, "message": message, "percent": percent, **extra}
    job.events.append({"event": event_type, "data": data})
    if _loop is not None and _loop.is_running():
        _loop.call_soon_threadsafe(job.event_flag.set)
    else:
        job.event_flag.set()


def _db_update(job_id: str, **kwargs) -> None:
    if _loop and _db:
        async def _do():
            await update_job_progress(_db, job_id, **kwargs)
        _loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_do()))


# ---------------------------------------------------------------------------
# Pipeline wrapper
# ---------------------------------------------------------------------------

def _run_pipeline(job: Job, asr_vendor: str, source_lang: str,
                  target_lang: str, translate: bool) -> None:
    from pipeline.adapter import asr_result_to_cues
    from pipeline.asr_factory import build_asr_client
    from extract_subtitles import write_srt, write_vtt, write_txt

    job.status = "processing"
    _db_update(job.job_id, status="processing", stage="audio_extract",
               message="正在从视频中提取音频...", percent=10)
    video_path = list((job.job_dir / "input").iterdir())[0]
    stem = video_path.stem
    src_short = source_lang.split("-", 1)[0]

    audio_tmp: Path | None = None
    tos_object_key: str | None = None
    tos_client = None

    try:
        # 1) extract audio
        _emit(job, "audio_extract", "正在从视频中提取音频...", 10)
        from pipeline.audio import AudioExtractor
        audio_dir = job.job_dir / "_audio_tmp"
        audio_tmp = audio_dir / f"{stem}.mp3"
        AudioExtractor().extract(video_path, audio_tmp)

        if asr_vendor == "ali":
            _emit(job, "asr_submit", "正在提交语音识别请求...", 35)
            _db_update(job.job_id, stage="asr_submit", message="正在提交语音识别请求...", percent=35)
            client = build_asr_client(vendor=asr_vendor, language=source_lang)
            _emit(job, "asr_poll", "等待语音识别结果...", 40)
            result = client.recognize(str(audio_tmp), language=source_lang)
        else:
            _emit(job, "tos_upload", "正在上传音频到云存储...", 25)
            from pipeline.tos_uploader import TosUploader
            tos_client = TosUploader.from_env()
            media_url, tos_object_key = tos_client.upload(audio_tmp, ttl_sec=3600)

            _emit(job, "asr_submit", "正在提交语音识别请求...", 35)
            client = build_asr_client(vendor=asr_vendor, language=source_lang)
            _emit(job, "asr_poll", "等待语音识别结果...", 40)
            result = client.recognize(media_url, language=source_lang)

        _emit(job, "asr_done",
              f"语音识别完成：{result.duration_sec:.0f}秒，{len(result.utterances)}段",
              75)
        _db_update(job.job_id, stage="asr_done", percent=75,
                   message=f"语音识别完成：{result.duration_sec:.0f}秒")

    except Exception as exc:
        if tos_client and tos_object_key:
            try: tos_client.delete(tos_object_key)
            except Exception: pass
        if audio_tmp and audio_tmp.exists():
            try: audio_tmp.unlink()
            except OSError: pass
        _emit(job, "error", f"处理失败: {exc}", 0, event_type="error", error=str(exc))
        job.status = "error"
        job.error = str(exc)
        _db_update(job.job_id, status="error", error=str(exc), stage="error")
        if _db:
            async def _fail():
                await fail_job(_db, job.job_id, str(exc))
            _loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_fail()))
        return

    # Cleanup TOS audio + local audio
    if tos_client and tos_object_key:
        try: tos_client.delete(tos_object_key)
        except Exception: pass
    if audio_tmp and audio_tmp.exists():
        try: audio_tmp.unlink()
        except OSError: pass
        try: audio_tmp.parent.rmdir()
        except OSError: pass

    # 5) write subtitles
    _emit(job, "subtitle_write", "正在生成字幕文件...", 80)
    out_dir = job.job_dir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)

    cues = asr_result_to_cues(result, max_chars_per_cue=30)
    src_srt = out_dir / f"{stem}_{src_short}.srt"
    src_vtt = out_dir / f"{stem}_{src_short}.vtt"
    src_txt = out_dir / f"{stem}_{src_short}.txt"
    write_srt(cues, src_srt)
    write_vtt(cues, src_vtt)
    write_txt(cues, src_txt)
    job.files = {"srt": True, "vtt": True, "txt": True}
    job.file_paths = {"srt": str(src_srt), "vtt": str(src_vtt), "txt": str(src_txt)}

    # 6) translate
    if translate and cues:
        _emit(job, "translate", f"正在翻译为{target_lang}...", 85)
        try:
            from pipeline.translator_volc import VolcTranslator
            tr_ak = os.environ.get("VOLC_TRANSLATE_AK")
            tr_sk = os.environ.get("VOLC_TRANSLATE_SK")
            if tr_ak and tr_sk:
                translator = VolcTranslator(access_key=tr_ak, secret_key=tr_sk)
                translated = translator.translate_cues(cues, target=target_lang, source=src_short)
                tgt_srt = out_dir / f"{stem}_{target_lang}.srt"
                tgt_vtt = out_dir / f"{stem}_{target_lang}.vtt"
                tgt_txt = out_dir / f"{stem}_{target_lang}.txt"
                write_srt(translated, tgt_srt)
                write_vtt(translated, tgt_vtt)
                write_txt(translated, tgt_txt)
                job.files.update({
                    "translated_srt": True,
                    "translated_vtt": True,
                    "translated_txt": True,
                })
                job.file_paths.update({
                    "translated_srt": str(tgt_srt),
                    "translated_vtt": str(tgt_vtt),
                    "translated_txt": str(tgt_txt),
                })
            else:
                _emit(job, "translate", "翻译跳过：未配置翻译密钥", 85)
        except Exception as exc:
            _emit(job, "translate", f"翻译失败: {exc}", 85)

    # 7) Upload subtitle files to TOS for persistent storage
    _emit(job, "saving", "正在保存字幕文件...", 95)
    file_uploads: list[tuple[str, str]] = []
    try:
        from pipeline.tos_uploader import TosUploader
        import tos as _tos
        sub_uploader = TosUploader.from_env(key_prefix="subtitles/")
        for fmt, path_str in job.file_paths.items():
            local_path = Path(path_str)
            if local_path.exists():
                _, tos_key = sub_uploader.upload(local_path)
                file_uploads.append((fmt, tos_key))
    except Exception:
        pass  # Best-effort TOS upload; local files still available

    # 8) Record to DB, then emit done
    if _db and file_uploads:
        async def _finalize():
            await complete_job(_db, job.job_id, file_uploads)
        _loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_finalize()))
    elif _db and not file_uploads:
        async def _finalize():
            await complete_job(_db, job.job_id, [])
        _loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_finalize()))

    _emit(job, "done", "完成", 100, event_type="complete", files=job.files)
    job.status = "complete"


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="字幕提取工具")


@app.on_event("startup")
async def _startup():
    global _loop, _db
    _loop = asyncio.get_running_loop()
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _db = await init_db(DB_PATH)
    await mark_interrupted_jobs(_db)


@app.get("/")
async def index():
    html = (Path(__file__).parent / "static" / "index.html").read_text("utf-8")
    return HTMLResponse(html)


@app.post("/api/jobs")
async def create_job(
    video: UploadFile = File(...),
    asr_vendor: str = Form("volc-bigmodel"),
    source_lang: str = Form("zh-CN"),
    target_lang: str = Form("en"),
    translate: str = Form("false"),
):
    if asr_vendor not in ("volc", "volc-bigmodel", "ali"):
        raise HTTPException(422, f"不支持的 ASR 引擎: {asr_vendor}")

    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / job_id
    input_dir = job_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)

    dest = input_dir / (video.filename or "video.mp4")
    size = 0
    with dest.open("wb") as f:
        while True:
            chunk = await video.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                shutil.rmtree(job_dir, ignore_errors=True)
                raise HTTPException(422, "文件超过 500MB 限制")
            f.write(chunk)

    job = Job(job_id=job_id, job_dir=job_dir)
    jobs[job_id] = job

    do_translate = translate.lower() in ("true", "1", "yes")

    # Insert to DB
    await insert_job(
        _db, id=job_id, video_filename=video.filename or "video.mp4",
        video_size=size, asr_vendor=asr_vendor, source_lang=source_lang,
        target_lang=target_lang, translate=do_translate,
    )

    t = threading.Thread(
        target=_run_pipeline,
        args=(job, asr_vendor, source_lang, target_lang, do_translate),
        daemon=True,
    )
    t.start()

    return {"job_id": job_id, "status": "queued"}


# ---------------------------------------------------------------------------
# History API
# ---------------------------------------------------------------------------

@app.get("/api/jobs")
async def list_history(limit: int = 20, offset: int = 0):
    items = await list_jobs(_db, limit=min(limit, 100), offset=offset)
    total = await count_jobs(_db)
    return {"items": items, "total": total}


@app.get("/api/jobs/{job_id}/status")
async def job_status(job_id: str):
    # Running job from memory
    job = jobs.get(job_id)
    if job:
        return {
            "job_id": job.job_id,
            "status": job.status,
            "stage": job.stage,
            "message": job.message,
            "percent": job.percent,
            "error": job.error,
            "files": job.files,
        }
    # Past job from DB
    row = await get_job(_db, job_id)
    if row:
        files_rows = await get_job_files(_db, job_id)
        row["files"] = {f["format"]: True for f in files_rows}
        return row
    raise HTTPException(404, "任务不存在")


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")

    from sse_starlette.sse import EventSourceResponse

    async def event_generator():
        idx = 0
        while True:
            while idx < len(job.events):
                evt = job.events[idx]
                idx += 1
                yield {"event": evt["event"], "data": json.dumps(evt["data"], ensure_ascii=False)}
            if job.status in ("complete", "error"):
                return
            job.event_flag.clear()
            await asyncio.get_running_loop().run_in_executor(None, job.event_flag.wait, 2.0)

    return EventSourceResponse(event_generator())


@app.get("/api/jobs/{job_id}/download/{fmt}")
async def download_result(job_id: str, fmt: str):
    if fmt not in FMT_MAP:
        raise HTTPException(422, f"不支持的格式: {fmt}")

    # Running job: serve from local temp dir
    job = jobs.get(job_id)
    if job and job.status == "processing":
        file_path = job.file_paths.get(fmt)
        if file_path and Path(file_path).exists():
            _, ext = FMT_MAP[fmt]
            media_type = "text/plain"
            if ext == ".srt": media_type = "application/x-subrip"
            elif ext == ".vtt": media_type = "text/vtt"
            return FileResponse(Path(file_path), media_type=media_type, filename=Path(file_path).name)

    # Completed job: serve from TOS via signed URL redirect
    files = await get_job_files(_db, job_id)
    file_row = next((f for f in files if f["format"] == fmt), None)
    if file_row:
        from pipeline.tos_uploader import TosUploader
        import tos as _tos
        uploader = TosUploader.from_env()
        signed = uploader.client.pre_signed_url(
            _tos.HttpMethodType.Http_Method_Get,
            uploader.bucket,
            file_row["tos_key"],
            expires=3600,
        )
        return RedirectResponse(url=signed.signed_url)

    raise HTTPException(404, "文件不存在")


# ---------------------------------------------------------------------------
# Periodic cleanup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def _start_cleanup():
    async def _cleanup_loop():
        while True:
            await asyncio.sleep(1800)
            now = time.time()
            # Clean up in-memory jobs and local temp dirs
            to_delete = [
                jid for jid, j in jobs.items()
                if now - j.job_dir.stat().st_mtime > JOB_TTL_SEC
            ]
            for jid in to_delete:
                job = jobs.pop(jid, None)
                if job:
                    shutil.rmtree(job.job_dir, ignore_errors=True)
            # Clean up old DB records (>30 days) and their TOS objects
            try:
                tos_keys = await cleanup_old_jobs(_db, days=30)
                if tos_keys:
                    from pipeline.tos_uploader import TosUploader
                    uploader = TosUploader.from_env()
                    for key in tos_keys:
                        try: uploader.delete(key)
                        except Exception: pass
            except Exception:
                pass
    asyncio.create_task(_cleanup_loop())
