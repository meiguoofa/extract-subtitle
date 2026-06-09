"""TOS uploader for staging audio files behind a public pre-signed URL.

Uses the official ``tos`` SDK so we don't reinvent V4 signing for object
storage. Bucket/endpoint are constructor parameters; AK/SK come from env
(``TOS_ACCESS_KEY_ID`` / ``TOS_SECRET_ACCESS_KEY``).
"""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import tos


class TosUploader:
    def __init__(
        self,
        access_key: str,
        secret_key: str,
        endpoint: str = "tos-cn-shanghai.volces.com",
        region: str = "cn-shanghai",
        bucket: str = "duanju123123",
        key_prefix: str = "asr_audio/",
    ) -> None:
        if not (access_key and secret_key):
            raise ValueError("access_key and secret_key are required")
        self.bucket = bucket
        self.key_prefix = key_prefix.lstrip("/")
        self.client = tos.TosClientV2(access_key, secret_key, endpoint, region)

    @classmethod
    def from_env(cls, **overrides) -> "TosUploader":
        ak = os.environ.get("TOS_ACCESS_KEY_ID")
        sk = os.environ.get("TOS_SECRET_ACCESS_KEY")
        if not (ak and sk):
            raise RuntimeError("TOS_ACCESS_KEY_ID / TOS_SECRET_ACCESS_KEY env vars are required")
        return cls(access_key=ak, secret_key=sk, **overrides)

    def upload(self, local_path: Path, ttl_sec: int = 3600) -> tuple[str, str]:
        """Upload file and return (signed_url, object_key)."""
        ts = int(time.time())
        rand = uuid.uuid4().hex[:8]
        object_key = f"{self.key_prefix}{ts}_{rand}_{local_path.name}"
        with local_path.open("rb") as f:
            self.client.put_object(self.bucket, object_key, content=f)
        signed = self.client.pre_signed_url(
            tos.HttpMethodType.Http_Method_Get,
            self.bucket,
            object_key,
            expires=ttl_sec,
        )
        return signed.signed_url, object_key

    def delete(self, object_key: str) -> None:
        try:
            self.client.delete_object(self.bucket, object_key)
        except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
            print(f"[tos] cleanup failed for {object_key}: {exc}")
