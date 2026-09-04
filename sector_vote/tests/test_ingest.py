from datetime import datetime, timezone

from sector_vote.ingest import refresh_channels
from sector_vote.storage import SectorStore
from sector_vote.transcript_provider import (
    ProviderAccessError,
    ProviderLimitExceeded,
    TranscriptUnavailable,
)


def test_refresh_channels_saves_only_recent_unseen_videos(tmp_path):
    store = SectorStore(tmp_path / "sector.db")
    channels = [{"name": "채널A", "channel_id": "a"}]
    videos = [
        {"video_id": "new", "title": "오늘 전망", "published_at": "2026-08-24T00:00:00+00:00", "url": "https://youtu.be/new"},
        {"video_id": "old", "title": "예전 전망", "published_at": "2026-08-20T00:00:00+00:00", "url": "https://youtu.be/old"},
    ]

    result = refresh_channels(
        store=store,
        channels=channels,
        now=datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc),
        lookback_hours=36,
        fetch_videos=lambda channel: videos,
        fetch_script=lambda video_id: "반도체가 강할 수 있습니다",
        classify=lambda transcript: [{
            "sector": "반도체", "direction": "up", "horizon": "next_session",
            "confidence": 0.8, "reason": "수요", "quote": transcript,
        }],
    )

    assert result["videos_analyzed"] == 1
    assert result["errors"] == []
    assert [row["video_id"] for row in store.list_calls()] == ["new"]


def test_refresh_stops_after_managed_provider_limit(tmp_path):
    store = SectorStore(tmp_path / "sector.db")
    attempts = []

    def limited(video_id):
        attempts.append(video_id)
        raise ProviderLimitExceeded("credits exhausted")

    result = refresh_channels(
        store=store,
        channels=[{"name": "A"}, {"name": "B"}],
        now=datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc),
        lookback_hours=36,
        fetch_videos=lambda channel: [{
            "video_id": channel["name"],
            "title": "전망",
            "published_at": "2026-08-24T00:00:00+00:00",
            "url": f"https://youtu.be/{channel['name']}",
        }],
        fetch_script=limited,
        classify=lambda _text: [],
    )

    assert attempts == ["A"]
    assert result["provider_limit_exceeded"] is True


def test_refresh_stops_after_managed_provider_auth_error(tmp_path):
    store = SectorStore(tmp_path / "sector.db")
    attempts = []

    def unauthorized(video_id):
        attempts.append(video_id)
        raise ProviderAccessError("invalid key")

    result = refresh_channels(
        store=store,
        channels=[{"name": "A"}, {"name": "B"}],
        now=datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc),
        lookback_hours=36,
        fetch_videos=lambda channel: [{
            "video_id": channel["name"],
            "title": "전망",
            "published_at": "2026-08-24T00:00:00+00:00",
            "url": f"https://youtu.be/{channel['name']}",
        }],
        fetch_script=unauthorized,
        classify=lambda _text: [],
    )

    assert attempts == ["A"]
    assert result["provider_access_error"] is True


def test_unavailable_transcript_is_charged_only_once_across_refreshes(tmp_path):
    store = SectorStore(tmp_path / "sector.db")
    attempts = []
    video = {
        "video_id": "silent",
        "title": "자막 없음",
        "published_at": "2026-08-24T00:00:00+00:00",
        "url": "https://youtu.be/silent",
    }

    def unavailable(video_id):
        attempts.append(video_id)
        raise TranscriptUnavailable("no native captions")

    kwargs = {
        "store": store,
        "channels": [{"name": "A"}],
        "now": datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc),
        "lookback_hours": 36,
        "fetch_videos": lambda _channel: [video],
        "fetch_script": unavailable,
        "classify": lambda _text: [],
    }

    refresh_channels(**kwargs)
    refresh_channels(**kwargs)

    assert attempts == ["silent"]
    assert store.list_video_ids() == {"silent"}
