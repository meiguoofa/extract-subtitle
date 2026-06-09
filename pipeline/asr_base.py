"""ASR client protocol and shared exceptions."""
from __future__ import annotations

from typing import Protocol

from .models import ASRResult


class ASRError(Exception):
    """Base for all ASR failures."""


class ASRSubmitError(ASRError):
    """Submitting the task failed."""


class ASRTimeoutError(ASRError):
    """Polling exceeded the deadline."""


class ASRRemoteError(ASRError):
    """Vendor returned a non-success status."""


class ASRClient(Protocol):
    """Vendor-agnostic ASR interface.

    Implementations accept either a public media URL or a local audio path
    (depending on vendor capability) and return a unified ``ASRResult``.
    """

    def recognize(self, media_url: str, language: str | None = None) -> ASRResult: ...
