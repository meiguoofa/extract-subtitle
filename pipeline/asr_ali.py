"""Aliyun NLS FlashRecognizer (极速版) — synchronous file recognition.

Supports Thai, Arabic and 50+ other languages. Two input modes:
- URL mode: pass a public audio/video URL via ``audio_address`` param
- Binary mode: POST local audio file directly (no object storage needed)

Auth: AccessKey ID/Secret → POP API CreateToken → Bearer token (24h TTL).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse
import uuid
from pathlib import Path

import requests

from .asr_base import ASRRemoteError, ASRSubmitError
from .models import ASRResult, ASRUtterance, ASRWord

FLASH_URL = "https://nls-gateway-cn-shanghai.aliyuncs.com/stream/v1/FlashRecognizer"
TOKEN_API = "https://nls-meta.cn-shanghai.aliyuncs.com/"

# Default language → AppKey mapping (override via constructor)
LANG_APPKEY_MAP: dict[str, str] = {
    "th-TH": "jju8ryhhSjIQVkJb",
    "th":    "jju8ryhhSjIQVkJb",
    "ar-SA": "FqBp1n6X5oSXFi9s",
    "ar":    "FqBp1n6X5oSXFi9s",
}


def _quote(s: str) -> str:
    return urllib.parse.quote(s, safe="")


class AliFlashASRClient:
    def __init__(
        self,
        access_key_id: str,
        access_key_secret: str,
        app_key: str | None = None,
        language_appkeys: dict[str, str] | None = None,
        region: str = "cn-shanghai",
        poll_interval: float = 3.0,
        poll_timeout: float = 600.0,
    ) -> None:
        self.access_key_id = access_key_id
        self.access_key_secret = access_key_secret
        self.app_key = app_key
        self.language_appkeys = {**LANG_APPKEY_MAP, **(language_appkeys or {})}
        self._token: str | None = None
        self._token_expire: float = 0

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def _fetch_token(self) -> tuple[str, float]:
        params: dict[str, str] = {
            "Action": "CreateToken",
            "Version": "2019-02-28",
            "Format": "JSON",
            "AccessKeyId": self.access_key_id,
            "SignatureMethod": "HMAC-SHA1",
            "SignatureVersion": "1.0",
            "SignatureNonce": str(uuid.uuid4()),
            "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        # POP API V1.0 signature
        sorted_params = sorted(params.items())
        canonical = "&".join(f"{_quote(k)}={_quote(v)}" for k, v in sorted_params)
        string_to_sign = "GET&%2F&" + _quote(canonical)
        key = (self.access_key_secret + "&").encode("utf-8")
        sig = base64.b64encode(
            hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha1).digest()
        ).decode("utf-8")
        params["Signature"] = sig

        try:
            resp = requests.get(TOKEN_API, params=params, timeout=10)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise ASRSubmitError(f"Ali token fetch failed: {exc}") from exc

        data = resp.json()
        # Response wraps token info under "Token" key
        token_obj = data.get("Token", data)
        token_id = token_obj.get("Id") or token_obj.get("Token") or token_obj.get("token", "")
        expire_time = token_obj.get("ExpireTime") or data.get("ExpireTime", 0)
        if isinstance(expire_time, (int, float)) and expire_time > 1e9:
            expire_time = float(expire_time)  # epoch seconds
        elif isinstance(expire_time, str):
            expire_time = float(expire_time)
        if not token_id:
            raise ASRSubmitError(f"Ali token response missing Token: {data}")
        return token_id, expire_time

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expire - 300:
            return self._token
        self._token, self._token_expire = self._fetch_token()
        return self._token

    def _get_appkey(self, language: str | None) -> str:
        if language:
            # Case-insensitive lookup
            lower_map = {k.lower(): v for k, v in self.language_appkeys.items()}
            key = language.lower()
            if key in lower_map:
                return lower_map[key]
            # Try short form: "th-TH" → "th"
            short = key.split("-")[0]
            if short in lower_map:
                return lower_map[short]
        if self.app_key:
            return self.app_key
        raise ValueError(
            f"No AppKey for language={language!r}. "
            f"Configured: {list(self.language_appkeys.keys())}"
        )

    @staticmethod
    def _detect_format(path_or_url: str) -> str:
        clean = path_or_url.split("?", 1)[0].lower()
        for ext in (".mp4", ".m4a", ".aac", ".mp3", ".opus", ".wav"):
            if clean.endswith(ext):
                return ext.lstrip(".")
        return "mp3"

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------

    def recognize(self, media_url: str, language: str | None = None) -> ASRResult:
        token = self._get_token()
        appkey = self._get_appkey(language)
        fmt = self._detect_format(media_url)

        if media_url.startswith("http"):
            return self._recognize_url(media_url, token, appkey, fmt)
        return self._recognize_file(media_url, token, appkey, fmt)

    def _recognize_url(self, url: str, token: str, appkey: str, fmt: str) -> ASRResult:
        params = {
            "appkey": appkey,
            "token": token,
            "format": fmt,
            "sample_rate": 16000,
            "enable_word_level_result": "true",
            "audio_address": url,
        }
        headers = {"Content-Type": "application/text"}
        try:
            resp = requests.post(FLASH_URL, params=params, headers=headers, timeout=300)
        except requests.RequestException as exc:
            raise ASRSubmitError(f"Ali Flash ASR request failed: {exc}") from exc
        return self._parse_response(resp)

    def _recognize_file(self, path: str, token: str, appkey: str, fmt: str) -> ASRResult:
        audio = Path(path).read_bytes()
        params = {
            "appkey": appkey,
            "token": token,
            "format": fmt,
            "sample_rate": 16000,
            "enable_word_level_result": "true",
        }
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(audio)),
        }
        try:
            resp = requests.post(
                FLASH_URL, params=params, headers=headers, data=audio, timeout=300
            )
        except requests.RequestException as exc:
            raise ASRSubmitError(f"Ali Flash ASR request failed: {exc}") from exc
        return self._parse_response(resp)

    @staticmethod
    def _parse_response(resp: requests.Response) -> ASRResult:
        try:
            data = resp.json()
        except ValueError:
            raise ASRRemoteError(f"Ali Flash ASR non-JSON response: {resp.text[:300]}")

        status = data.get("status")
        if status != 20000000:
            msg = data.get("message", resp.text[:200])
            raise ASRRemoteError(f"Ali Flash ASR error: status={status}, {msg}")

        flash = data.get("flash_result") or {}
        duration_ms = flash.get("duration", 0)
        utterances: list[ASRUtterance] = []
        for s in flash.get("sentences") or []:
            words = [
                ASRWord(
                    text=w.get("text", ""),
                    start_ms=int(w.get("begin_time", 0)),
                    end_ms=int(w.get("end_time", 0)),
                )
                for w in s.get("words") or []
            ]
            utterances.append(
                ASRUtterance(
                    text=s.get("text", ""),
                    start_ms=int(s.get("begin_time", 0)),
                    end_ms=int(s.get("end_time", 0)),
                    words=words,
                )
            )
        return ASRResult(
            utterances=utterances,
            detected_lang="",
            duration_sec=float(duration_ms) / 1000.0,
            raw=data,
        )
