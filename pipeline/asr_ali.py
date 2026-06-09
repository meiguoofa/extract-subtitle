"""Aliyun NLS recording file recognition — placeholder.

Interface and constructor parameters are finalized so the framework, CLI
routing, and tests can be wired now. The real submit/poll implementation
is scheduled for a later phase (covers Thai/Arabic which Volcengine
Doubao does not list).
"""
from __future__ import annotations

from .asr_base import ASRError
from .models import ASRResult


class AliNLSASRClient:
    BASE_URL = "https://nls-filetrans.cn-shanghai.aliyuncs.com"

    def __init__(
        self,
        access_key_id: str,
        access_key_secret: str,
        app_key: str,
        region: str = "cn-shanghai",
        poll_interval: float = 3.0,
        poll_timeout: float = 600.0,
    ) -> None:
        self.access_key_id = access_key_id
        self.access_key_secret = access_key_secret
        self.app_key = app_key
        self.region = region
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout

    def recognize(self, media_url: str, language: str | None = None) -> ASRResult:
        raise NotImplementedError(
            "Aliyun NLS client is not yet wired. Use --asr-vendor volc for now. "
            "Aliyun support is planned for Thai/Arabic coverage."
        )
