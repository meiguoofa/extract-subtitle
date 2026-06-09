"""Volcengine Doubao Video Captioning client (vc/submit + vc/query).

Reference: https://www.volcengine.com/docs/6561/80909
Auth: x-api-key header.
Flow: POST vc/submit -> returns task id -> poll GET vc/query?id=<id> until code==0 and utterances populated.
"""
from __future__ import annotations

import time
from typing import Any

import requests

from .asr_base import ASRRemoteError, ASRSubmitError, ASRTimeoutError
from .models import ASRResult, ASRUtterance, ASRWord


class VolcDoubaoVCClient:
    SUBMIT_URL = "https://openspeech.bytedance.com/api/v1/vc/submit"
    QUERY_URL = "https://openspeech.bytedance.com/api/v1/vc/query"

    def __init__(
        self,
        api_key: str,
        language: str = "zh-CN",
        use_itn: bool = True,
        use_capitalize: bool = True,
        max_lines: int = 1,
        words_per_line: int = 15,
        poll_interval: float = 3.0,
        poll_timeout: float = 600.0,
        request_timeout: float = 30.0,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self.api_key = api_key
        self.language = language
        self.use_itn = use_itn
        self.use_capitalize = use_capitalize
        self.max_lines = max_lines
        self.words_per_line = words_per_line
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        self.request_timeout = request_timeout

    def recognize(self, media_url: str, language: str | None = None) -> ASRResult:
        task_id = self._submit(media_url, language or self.language)
        raw = self._poll(task_id)
        return self._to_result(raw, language or self.language)

    def _submit(self, media_url: str, language: str) -> str:
        params = {
            "language": language,
            "use_itn": str(self.use_itn),
            "use_capitalize": str(self.use_capitalize),
            "max_lines": self.max_lines,
            "words_per_line": self.words_per_line,
        }
        headers = {
            "Accept": "*/*",
            "x-api-key": self.api_key,
            "content-type": "application/json",
        }
        try:
            resp = requests.post(
                self.SUBMIT_URL,
                params=params,
                headers=headers,
                json={"url": media_url},
                timeout=self.request_timeout,
            )
        except requests.RequestException as exc:
            raise ASRSubmitError(f"submit network error: {exc}") from exc

        if resp.status_code != 200:
            raise ASRSubmitError(
                f"submit HTTP {resp.status_code}: {resp.text[:500]}"
            )
        body = resp.json()
        if body.get("code") != 0:
            raise ASRSubmitError(f"submit code={body.get('code')}: {body.get('message')}")
        task_id = body.get("id")
        if not task_id:
            raise ASRSubmitError(f"submit returned no id: {body}")
        return task_id

    def _poll(self, task_id: str) -> dict[str, Any]:
        headers = {"Accept": "*/*", "x-api-key": self.api_key}
        deadline = time.monotonic() + self.poll_timeout
        while True:
            try:
                resp = requests.get(
                    self.QUERY_URL,
                    params={"id": task_id},
                    headers=headers,
                    timeout=self.request_timeout,
                )
            except requests.RequestException as exc:
                if time.monotonic() >= deadline:
                    raise ASRTimeoutError(f"query exhausted on network error: {exc}") from exc
                time.sleep(self.poll_interval)
                continue

            if resp.status_code != 200:
                raise ASRRemoteError(
                    f"query HTTP {resp.status_code}: {resp.text[:500]}"
                )
            body = resp.json()
            code = body.get("code")
            if code == 0 and body.get("utterances") is not None:
                return body
            if code not in (0, 1, 2000):
                raise ASRRemoteError(f"query code={code}: {body.get('message')}")

            if time.monotonic() >= deadline:
                raise ASRTimeoutError(
                    f"polling exceeded {self.poll_timeout}s for task {task_id}"
                )
            time.sleep(self.poll_interval)

    def _to_result(self, raw: dict[str, Any], language: str) -> ASRResult:
        utterances: list[ASRUtterance] = []
        for u in raw.get("utterances") or []:
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
        detected = (
            raw.get("attribute", {}).get("extra", {}).get("language") or language
        )
        return ASRResult(
            utterances=utterances,
            detected_lang=detected,
            duration_sec=float(raw.get("duration", 0.0)),
            raw=raw,
        )
