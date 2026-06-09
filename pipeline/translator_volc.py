"""Volcengine machine translation client (TranslateText, signature V4).

API:  POST https://translate.volcengineapi.com/?Action=TranslateText&Version=2020-06-01
Auth: AK/SK signature V4 (Service=translate, Region=cn-north-1).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
from dataclasses import dataclass

import requests

from .models import SubtitleCue


@dataclass
class TranslatedItem:
    src_text: str
    dst_text: str
    detected_src_lang: str


class VolcTranslator:
    SERVICE = "translate"
    HOST = "translate.volcengineapi.com"
    REGION = "cn-north-1"
    ACTION = "TranslateText"
    VERSION = "2020-06-01"

    def __init__(
        self,
        access_key: str,
        secret_key: str,
        timeout: float = 15.0,
        max_retries: int = 3,
        max_batch_items: int = 16,
        max_batch_chars: int = 4500,
    ) -> None:
        if not access_key or not secret_key:
            raise ValueError("access_key and secret_key are required")
        self.ak = access_key
        self.sk = secret_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_batch_items = max_batch_items
        self.max_batch_chars = max_batch_chars
        self._cache: dict[tuple[str, str, str | None], str] = {}

    def translate_cues(
        self,
        cues: list[SubtitleCue],
        target: str = "en",
        source: str | None = None,
    ) -> list[SubtitleCue]:
        if not cues:
            return []
        texts = [c.text for c in cues]
        translated = self.translate_texts(texts, target=target, source=source)
        return [
            SubtitleCue(start=cue.start, end=cue.end, text=item.dst_text or cue.text)
            for cue, item in zip(cues, translated)
        ]

    def translate_texts(
        self,
        texts: list[str],
        target: str = "en",
        source: str | None = None,
    ) -> list[TranslatedItem]:
        out: list[TranslatedItem | None] = [None] * len(texts)
        pending_idx: list[int] = []
        pending_text: list[str] = []
        for i, t in enumerate(texts):
            cache_key = (t, target, source)
            if cache_key in self._cache:
                out[i] = TranslatedItem(src_text=t, dst_text=self._cache[cache_key], detected_src_lang=source or "")
            else:
                pending_idx.append(i)
                pending_text.append(t)

        for batch_indices, batch_texts in self._iter_batches(pending_idx, pending_text):
            try:
                results = self._call_api(batch_texts, target=target, source=source)
            except Exception as exc:  # noqa: BLE001 — fail-soft: preserve original
                for idx, txt in zip(batch_indices, batch_texts):
                    out[idx] = TranslatedItem(src_text=txt, dst_text=txt, detected_src_lang=source or "")
                print(f"[translate] batch failed, keep original: {exc}")
                continue
            for idx, txt, item in zip(batch_indices, batch_texts, results):
                out[idx] = item
                self._cache[(txt, target, source)] = item.dst_text
        return [item or TranslatedItem(src_text=texts[i], dst_text=texts[i], detected_src_lang=source or "") for i, item in enumerate(out)]

    def _iter_batches(self, indices: list[int], texts: list[str]):
        cur_idx: list[int] = []
        cur_txt: list[str] = []
        cur_chars = 0
        for idx, txt in zip(indices, texts):
            if cur_idx and (
                len(cur_idx) >= self.max_batch_items
                or cur_chars + len(txt) > self.max_batch_chars
            ):
                yield cur_idx, cur_txt
                cur_idx, cur_txt, cur_chars = [], [], 0
            cur_idx.append(idx)
            cur_txt.append(txt)
            cur_chars += len(txt)
        if cur_idx:
            yield cur_idx, cur_txt

    def _call_api(self, texts: list[str], target: str, source: str | None) -> list[TranslatedItem]:
        body = {"TargetLanguage": target, "TextList": texts}
        if source:
            body["SourceLanguage"] = source
        body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")

        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                headers = self._sign(body_bytes)
                resp = requests.post(
                    f"https://{self.HOST}/",
                    params={"Action": self.ACTION, "Version": self.VERSION},
                    headers=headers,
                    data=body_bytes,
                    timeout=self.timeout,
                )
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                data = resp.json()
                if "ResponseMetadata" in data and data["ResponseMetadata"].get("Error"):
                    err = data["ResponseMetadata"]["Error"]
                    raise RuntimeError(f"vendor error {err.get('Code')}: {err.get('Message')}")
                tr_list = data.get("TranslationList") or []
                return [
                    TranslatedItem(
                        src_text=src,
                        dst_text=item.get("Translation", ""),
                        detected_src_lang=item.get("DetectedSourceLanguage", source or ""),
                    )
                    for src, item in zip(texts, tr_list)
                ]
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < self.max_retries - 1:
                    continue
                raise
        raise last_exc  # type: ignore[misc]

    def _sign(self, body: bytes) -> dict[str, str]:
        now = _dt.datetime.now(_dt.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        content_type = "application/json"
        payload_hash = hashlib.sha256(body).hexdigest()

        query_string = f"Action={self.ACTION}&Version={self.VERSION}"
        canonical_headers = (
            f"content-type:{content_type}\n"
            f"host:{self.HOST}\n"
            f"x-content-sha256:{payload_hash}\n"
            f"x-date:{amz_date}\n"
        )
        signed_headers = "content-type;host;x-content-sha256;x-date"
        canonical_request = "\n".join(
            [
                "POST",
                "/",
                query_string,
                canonical_headers,
                signed_headers,
                payload_hash,
            ]
        )

        algorithm = "HMAC-SHA256"
        credential_scope = f"{date_stamp}/{self.REGION}/{self.SERVICE}/request"
        string_to_sign = "\n".join(
            [
                algorithm,
                amz_date,
                credential_scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )

        k_date = hmac.new(self.sk.encode("utf-8"), date_stamp.encode("utf-8"), hashlib.sha256).digest()
        k_region = hmac.new(k_date, self.REGION.encode("utf-8"), hashlib.sha256).digest()
        k_service = hmac.new(k_region, self.SERVICE.encode("utf-8"), hashlib.sha256).digest()
        k_signing = hmac.new(k_service, b"request", hashlib.sha256).digest()
        signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        authorization = (
            f"{algorithm} Credential={self.ak}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return {
            "Content-Type": content_type,
            "Host": self.HOST,
            "X-Date": amz_date,
            "X-Content-Sha256": payload_hash,
            "Authorization": authorization,
        }
