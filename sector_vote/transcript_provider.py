"""Managed transcript providers for cloud-safe YouTube ingestion."""

import re
import time
from collections.abc import Callable

import requests

SUPADATA_TRANSCRIPT_URL = "https://api.supadata.ai/v1/transcript"
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,30}$")
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,100}$")


class TranscriptUnavailable(RuntimeError):
    """The provider could not supply a native transcript."""


class ProviderLimitExceeded(RuntimeError):
    """The managed provider rate or credit limit has been reached."""


class ProviderAccessError(RuntimeError):
    """The managed provider rejected the configured API credentials."""


class ProviderProtocolError(RuntimeError):
    """The managed provider returned an unsupported protocol response."""


def _transcript_text(payload: dict) -> str:
    content = payload.get("content")
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        text = " ".join(
            str(chunk.get("text", "")).strip()
            for chunk in content
            if isinstance(chunk, dict) and str(chunk.get("text", "")).strip()
        )
    else:
        text = ""
    if not text:
        raise TranscriptUnavailable("Supadata returned no spoken transcript")
    return text


def _provider_error(payload: object, status_code: int) -> str:
    if isinstance(payload, dict):
        value = payload.get("error") or payload.get("message") or payload.get("detail")
        if isinstance(value, str) and value.strip():
            return value.strip()[:180]
        if isinstance(value, dict):
            parts = [value.get("code"), value.get("message"), value.get("details")]
            message = ": ".join(str(part).strip() for part in parts if part)
            if message:
                return message[:180]
    return f"HTTP {status_code}"


def _raise_provider_status(response, payload: object) -> None:
    status_code = response.status_code
    if 300 <= status_code < 400:
        raise ProviderProtocolError(f"Supadata redirected with HTTP {status_code}")
    if status_code == 206:
        raise TranscriptUnavailable(_provider_error(payload, status_code))
    if status_code == 429:
        raise ProviderLimitExceeded(_provider_error(payload, status_code))
    if status_code in {401, 402, 403}:
        raise ProviderAccessError(_provider_error(payload, status_code))
    response.raise_for_status()


def fetch_supadata_transcript(
    video_id: str,
    api_key: str,
    *,
    timeout: int = 45,
    max_polls: int = 90,
    poll_seconds: float = 1,
    session=requests,
    sleeper: Callable[[float], None] = time.sleep,
    cancel_event=None,
) -> str:
    """Fetch an existing Korean/English YouTube transcript via Supadata."""
    if not api_key:
        raise ValueError("SUPADATA_API_KEY is required")
    if not _VIDEO_ID_RE.fullmatch(video_id or ""):
        raise ValueError("invalid YouTube video id")

    headers = {"x-api-key": api_key}
    response = session.get(
        SUPADATA_TRANSCRIPT_URL,
        headers=headers,
        params={
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "lang": "ko",
            "text": "true",
            "mode": "native",
        },
        timeout=timeout,
        allow_redirects=False,
    )
    try:
        payload = response.json()
    except (TypeError, ValueError):
        payload = {}

    if response.status_code != 202:
        _raise_provider_status(response, payload)
        return _transcript_text(payload)

    job_id = payload.get("jobId") if isinstance(payload, dict) else None
    if not isinstance(job_id, str) or not _JOB_ID_RE.fullmatch(job_id):
        raise RuntimeError("Supadata returned an invalid asynchronous job id")

    job_url = f"{SUPADATA_TRANSCRIPT_URL}/{job_id}"
    for poll_index in range(max_polls):
        job_response = session.get(
            job_url,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
        )
        try:
            job_payload = job_response.json()
        except (TypeError, ValueError):
            job_payload = {}
        _raise_provider_status(job_response, job_payload)
        status = job_payload.get("status") if isinstance(job_payload, dict) else None
        if status == "completed":
            result_payload = job_payload.get("result") if isinstance(job_payload, dict) else None
            if not isinstance(result_payload, dict):
                result_payload = job_payload
            return _transcript_text(result_payload)
        if status == "failed":
            raise TranscriptUnavailable(_provider_error(job_payload, job_response.status_code))
        if poll_index < max_polls - 1:
            if cancel_event is not None:
                if cancel_event.wait(poll_seconds):
                    raise InterruptedError("Supadata polling cancelled during shutdown")
            else:
                sleeper(poll_seconds)

    raise TimeoutError("Supadata transcript job did not finish before the polling limit")
