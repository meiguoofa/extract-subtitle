"""Volcengine Doubao SeedASR BigModel client.

API:    POST /api/v3/auc/bigmodel/submit  (submit job, body returns {})
        POST /api/v3/auc/bigmodel/query   (poll, header carries x-api-status-code)
Auth:   x-api-key + X-Api-Resource-Id=volc.seedasr.auc + X-Api-Request-Id=<uuid4>
        The client-generated X-Api-Request-Id is the implicit task id; query
        with the same header to fetch its result.
"""
from __future__ import annotations

import time
import uuid

import requests

from .asr_base import ASRRemoteError, ASRSubmitError, ASRTimeoutError
from .models import ASRResult, ASRUtterance, ASRWord

SUBMIT_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
QUERY_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
RESOURCE_ID = "volc.seedasr.auc"

# Volc business status codes returned via header `x-api-status-code`
STATUS_SUCCESS = "20000000"
STATUS_IN_PROGRESS = {"20000001", "20000002"}  # queued/running (best-effort)


class VolcBigModelASRClient:
    def __init__(
        self,
        api_key: str,
        model_name: str = "bigmodel",
        enable_itn: bool = True,
        enable_punc: bool = True,
        enable_ddc: bool = False,
        show_utterances: bool = True,
        poll_interval: float = 3.0,
        poll_timeout: float = 600.0,
        request_timeout: float = 30.0,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self.api_key = api_key
        self.model_name = model_name
        self.enable_itn = enable_itn
        self.enable_punc = enable_punc
        self.enable_ddc = enable_ddc
        self.show_utterances = show_utterances
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        self.request_timeout = request_timeout

    def recognize(self, media_url: str, language: str | None = None) -> ASRResult:
        """Submit and poll. ``media_url`` must be a publicly reachable audio URL.

        ``language`` is accepted for protocol parity but BigModel auto-detects.
        """
        task_id = str(uuid.uuid4())
        self._submit(task_id, media_url)
        raw = self._poll(task_id)
        return self._to_result(raw, language or "")

    def _headers(self, task_id: str) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "X-Api-Resource-Id": RESOURCE_ID,
            "X-Api-Request-Id": task_id,
            "X-Api-Sequence": "-1",
        }

    def _submit(self, task_id: str, media_url: str) -> None:
        body = {
            "user": {"uid": "subtitle-extractor"},
            "audio": {
                "url": media_url,
                "format": "mp3",
                "codec": "raw",
                "rate": 16000,
                "bits": 16,
                "channel": 1,
            },
            "request": {
                "model_name": self.model_name,
                "enable_itn": self.enable_itn,
                "enable_punc": self.enable_punc,
                "enable_ddc": self.enable_ddc,
                "show_utterances": self.show_utterances,
            },
        }
        try:
            resp = requests.post(
                SUBMIT_URL, headers=self._headers(task_id), json=body, timeout=self.request_timeout
            )
        except requests.RequestException as exc:
            raise ASRSubmitError(f"submit network error: {exc}") from exc

        status = resp.headers.get("x-api-status-code", "")
        if status != STATUS_SUCCESS:
            msg = resp.headers.get("x-api-message", "") or resp.text[:300]
            raise ASRSubmitError(f"submit failed status={status}: {msg}")

    def _poll(self, task_id: str) -> dict:
        deadline = time.monotonic() + self.poll_timeout
        while True:
            try:
                resp = requests.post(
                    QUERY_URL,
                    headers=self._headers(task_id),
                    json={},
                    timeout=self.request_timeout,
                )
            except requests.RequestException as exc:
                if time.monotonic() >= deadline:
                    raise ASRTimeoutError(f"query exhausted on network error: {exc}") from exc
                time.sleep(self.poll_interval)
                continue

            status = resp.headers.get("x-api-status-code", "")
            if status == STATUS_SUCCESS:
                body = resp.json() if resp.content else {}
                if body.get("result"):
                    return body
                # status OK but no result yet — keep polling
            elif status in STATUS_IN_PROGRESS or status.startswith("2000000"):
                pass
            else:
                msg = resp.headers.get("x-api-message", "") or resp.text[:300]
                raise ASRRemoteError(f"query failed status={status}: {msg}")

            if time.monotonic() >= deadline:
                raise ASRTimeoutError(
                    f"polling exceeded {self.poll_timeout}s for task {task_id}"
                )
            time.sleep(self.poll_interval)

    def _to_result(self, raw: dict, language: str) -> ASRResult:
        result = raw.get("result") or {}
        utterances: list[ASRUtterance] = []
        for u in result.get("utterances") or []:
            words = [
                ASRWord(
                    text=w.get("text", ""),
                    start_ms=int(w.get("start_time", 0)),
                    end_ms=int(w.get("end_time", 0)),
                )
                for w in (u.get("words") or [])
            ]
            utterances.append(
                ASRUtterance(
                    text=u.get("text", ""),
                    start_ms=int(u.get("start_time", 0)),
                    end_ms=int(u.get("end_time", 0)),
                    words=words,
                )
            )
        duration_ms = (raw.get("audio_info") or {}).get("duration", 0)
        return ASRResult(
            utterances=utterances,
            detected_lang=language,
            duration_sec=float(duration_ms) / 1000.0,
            raw=raw,
        )
