from datetime import datetime, timezone

from sector_vote.ingest import refresh_channels
from sector_vote.storage import SectorStore


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
