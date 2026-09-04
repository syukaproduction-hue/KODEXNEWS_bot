"""Channel refresh pipeline with dependency injection for tests."""

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from sector_vote.storage import SectorStore
from sector_vote.transcript_provider import (
    ProviderAccessError,
    ProviderLimitExceeded,
    TranscriptUnavailable,
)


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def refresh_channels(
    *,
    store: SectorStore,
    channels: list[dict],
    now: datetime,
    lookback_hours: int,
    fetch_videos: Callable[[dict], list[dict]],
    fetch_script: Callable[[str], str],
    classify: Callable[[str], list[dict]],
    max_videos_per_channel: int = 2,
    clock: Callable[[], datetime] | None = None,
) -> dict:
    cutoff = now.astimezone(timezone.utc) - timedelta(hours=lookback_hours)
    operation_clock = clock or (lambda: datetime.now(timezone.utc))
    result = {
        "channels_checked": 0,
        "videos_analyzed": 0,
        "calls_saved": 0,
        "errors": [],
        "provider_limit_exceeded": False,
        "provider_access_error": False,
    }

    for channel in channels:
        result["channels_checked"] += 1
        try:
            videos = fetch_videos(channel)
        except Exception as exc:  # noqa: BLE001 - isolate per-channel network failures
            result["errors"].append({"channel": channel["name"], "error": str(exc)[:180]})
            continue

        eligible = [video for video in videos if _parse_iso(video["published_at"]) >= cutoff]
        for video in eligible[:max_videos_per_channel]:
            if not store.claim_video(video["video_id"], operation_clock()):
                continue
            try:
                transcript = fetch_script(video["video_id"])
                calls = classify(transcript)
                store.save_video_calls(
                    video_id=video["video_id"],
                    channel=channel["name"],
                    title=video["title"],
                    url=video["url"],
                    published_at=video["published_at"],
                    calls=calls,
                )
                result["videos_analyzed"] += 1
                result["calls_saved"] += len(calls)
            except Exception as exc:  # noqa: BLE001 - isolate per-channel network failures
                if isinstance(exc, TranscriptUnavailable):
                    store.mark_video_attempt(
                        video["video_id"], status="unavailable", now=operation_clock(), error=str(exc)
                    )
                else:
                    store.mark_video_attempt(
                        video["video_id"], status="failed", now=operation_clock(), retry_minutes=360, error=str(exc)
                    )
                result["errors"].append({
                    "channel": channel["name"],
                    "video_id": video["video_id"],
                    "error": str(exc)[:180],
                })
                if isinstance(exc, ProviderLimitExceeded):
                    result["provider_limit_exceeded"] = True
                    return result
                if isinstance(exc, ProviderAccessError):
                    result["provider_access_error"] = True
                    return result
    return result
