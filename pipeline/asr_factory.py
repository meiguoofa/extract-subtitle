"""Vendor factory: pick an ASR client by name, reading credentials from env."""
from __future__ import annotations

import os

from .asr_ali import AliFlashASRClient
from .asr_base import ASRClient
from .asr_volc import VolcDoubaoVCClient
from .asr_volc_bigmodel import VolcBigModelASRClient


def build_asr_client(
    vendor: str,
    language: str | None = None,
    poll_interval: float | None = None,
    poll_timeout: float | None = None,
    api_key_override: str | None = None,
) -> ASRClient:
    vendor = (vendor or "").lower()
    if vendor in ("volc", "volc-vc", "doubao-vc", "volcengine"):
        api_key = api_key_override or os.environ.get("VOLC_DOUBAO_API_KEY")
        if not api_key:
            raise RuntimeError(
                "VOLC_DOUBAO_API_KEY is required (env var or --volc-api-key)"
            )
        kwargs: dict = {"api_key": api_key}
        if language:
            kwargs["language"] = language
        if poll_interval is not None:
            kwargs["poll_interval"] = poll_interval
        if poll_timeout is not None:
            kwargs["poll_timeout"] = poll_timeout
        return VolcDoubaoVCClient(**kwargs)

    if vendor in ("volc-bigmodel", "bigmodel", "seedasr", "doubao"):
        api_key = api_key_override or os.environ.get("VOLC_DOUBAO_API_KEY")
        if not api_key:
            raise RuntimeError(
                "VOLC_DOUBAO_API_KEY is required (env var or --volc-api-key)"
            )
        kwargs = {"api_key": api_key}
        if poll_interval is not None:
            kwargs["poll_interval"] = poll_interval
        if poll_timeout is not None:
            kwargs["poll_timeout"] = poll_timeout
        return VolcBigModelASRClient(**kwargs)

    if vendor in ("ali", "aliyun", "alibaba"):
        ak = os.environ.get("ALI_ACCESS_KEY_ID")
        sk = os.environ.get("ALI_ACCESS_KEY_SECRET")
        if not (ak and sk):
            raise RuntimeError(
                "Aliyun NLS needs ALI_ACCESS_KEY_ID / ALI_ACCESS_KEY_SECRET"
            )
        # Build language→appkey map from env
        language_appkeys: dict[str, str] = {}
        for key, val in os.environ.items():
            if key.startswith("ALI_APP_KEY_"):
                lang = key[len("ALI_APP_KEY_"):].lower().replace("_", "-")
                language_appkeys[lang] = val
        default_appkey = os.environ.get("ALI_APP_KEY")
        return AliFlashASRClient(
            access_key_id=ak,
            access_key_secret=sk,
            app_key=default_appkey,
            language_appkeys=language_appkeys if language_appkeys else None,
        )

    raise ValueError(f"Unknown ASR vendor: {vendor!r}. Choose 'volc', 'volc-bigmodel' or 'ali'.")
