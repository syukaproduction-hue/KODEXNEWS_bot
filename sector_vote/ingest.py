"""Channel refresh pipeline with dependency injection for tests."""

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from sector_vote.storage import SectorStore


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
) -> dict:
    cutoff = now.astimezone(timezone.utc) - timedelta(hours=lookback_hours)
    result = {"channels_checked": 0, "videos_analyzed": 0, "calls_saved": 0, "errors": []}

    for channel in channels:
        result["channels_checked"] += 1
        try:
            videos = fetch_videos(channel)
        except Exception as exc:  # noqa: BLE001 - isolate per-channel network failures
            result["errors"].append({"channel": channel["name"], "error": str(exc)[:180]})
            continue

        eligible = [video for video in videos if _parse_iso(video["published_at"]) >= cutoff]
        for video in eligible[:max_videos_per_channel]:
            if store.has_video(video["video_id"]):
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
                result["errors"].append({
                    "channel": channel["name"],
                    "video_id": video["video_id"],
                    "error": str(exc)[:180],
                })
    return result
