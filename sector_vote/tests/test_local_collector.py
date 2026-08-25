from datetime import datetime, timezone

import pytest

from sector_vote.local_collector import (
    collect_and_upload,
    collection_exit_code,
    fetch_known_video_ids,
    load_batch_start,
    normalize_service_url,
    save_batch_start,
    select_channel_batch,
    validate_cli_numbers,
)


def test_collector_uploads_recent_unseen_transcripts_and_skips_known():
    channels = [{"name": "채널A", "channel_id": "a"}]
    videos = [
        {"video_id": "new", "title": "오늘 전망", "published_at": "2026-08-24T00:00:00+00:00", "url": "https://youtu.be/new"},
        {"video_id": "known", "title": "이미 처리", "published_at": "2026-08-24T00:00:00+00:00", "url": "https://youtu.be/known"},
        {"video_id": "old", "title": "오래된 영상", "published_at": "2026-08-20T00:00:00+00:00", "url": "https://youtu.be/old"},
    ]
    uploads = []

    result = collect_and_upload(
        channels=channels,
        service_url="https://sector.example",
        token="secret",
        now=datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc),
        lookback_hours=36,
        known_video_ids={"known"},
        fetch_videos=lambda _channel: videos,
        fetch_script=lambda video_id: f"{video_id} transcript",
        upload=lambda url, token, payload: uploads.append((url, token, payload)),
    )

    assert result["videos_uploaded"] == 1
    assert result["videos_skipped"] == 2
    assert result["errors"] == []
    assert uploads[0][0] == "https://sector.example/api/ingest/transcript"
    assert uploads[0][1] == "secret"
    assert uploads[0][2]["video_id"] == "new"
    assert uploads[0][2]["channel"] == "채널A"


def test_collector_continues_after_transcript_failure():
    channels = [{"name": "채널A", "channel_id": "a"}]
    videos = [{
        "video_id": "blocked", "title": "전망",
        "published_at": "2026-08-24T00:00:00+00:00", "url": "https://youtu.be/blocked",
    }]

    def fail(_video_id):
        raise RuntimeError("captions unavailable")

    result = collect_and_upload(
        channels=channels,
        service_url="https://sector.example/",
        token="secret",
        now=datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc),
        lookback_hours=36,
        known_video_ids=set(),
        fetch_videos=lambda _channel: videos,
        fetch_script=fail,
        upload=lambda *_args: None,
    )

    assert result["videos_uploaded"] == 0
    assert result["errors"][0]["video_id"] == "blocked"


def test_service_url_requires_clean_https_origin():
    assert normalize_service_url("https://sector.example/") == "https://sector.example"
    with pytest.raises(ValueError):
        normalize_service_url("http://sector.example")
    with pytest.raises(ValueError):
        normalize_service_url("https://user:pass@sector.example")
    with pytest.raises(ValueError):
        normalize_service_url("https://sector.example/path?next=evil")


def test_authenticated_requests_reject_redirects(monkeypatch):
    class RedirectResponse:
        status_code = 302

        def raise_for_status(self):
            return None

    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return RedirectResponse()

    monkeypatch.setattr("sector_vote.local_collector.requests.get", fake_get)

    with pytest.raises(RuntimeError, match="리다이렉트"):
        fetch_known_video_ids("https://sector.example", "secret")

    assert captured["url"] == "https://sector.example/api/videos"
    assert captured["allow_redirects"] is False


def test_collector_isolates_malformed_video_timestamp():
    videos = [
        {"video_id": "bad", "title": "오류", "published_at": "not-a-date", "url": "https://youtu.be/bad"},
        {"video_id": "good", "title": "정상", "published_at": "2026-08-24T00:00:00+00:00", "url": "https://youtu.be/good"},
    ]
    uploads = []

    result = collect_and_upload(
        channels=[{"name": "채널A", "channel_id": "a"}],
        service_url="https://sector.example",
        token="secret",
        now=datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc),
        lookback_hours=36,
        known_video_ids=set(),
        fetch_videos=lambda _channel: videos,
        fetch_script=lambda _video_id: "정상 자막입니다. 다음 거래일 반도체 전망입니다.",
        upload=lambda _url, _token, payload: uploads.append(payload["video_id"]),
    )

    assert uploads == ["good"]
    assert result["errors"][0]["video_id"] == "bad"


def test_completed_noop_run_is_success():
    assert collection_exit_code({"videos_uploaded": 0, "errors": []}) == 0


def test_channel_batches_rotate_without_overlap():
    channels = [{"name": str(i)} for i in range(20)]

    first, next_start = select_channel_batch(channels, start=0, batch_size=5)
    last, wrapped_start = select_channel_batch(channels, start=15, batch_size=5)

    assert [row["name"] for row in first] == ["0", "1", "2", "3", "4"]
    assert next_start == 5
    assert [row["name"] for row in last] == ["15", "16", "17", "18", "19"]
    assert wrapped_start == 0


def test_collector_stops_immediately_when_ip_is_blocked():
    class IpBlocked(Exception):
        pass

    videos = [
        {"video_id": "one", "title": "1", "published_at": "2026-08-24T00:00:00+00:00", "url": "https://youtu.be/one"},
        {"video_id": "two", "title": "2", "published_at": "2026-08-24T00:00:00+00:00", "url": "https://youtu.be/two"},
    ]
    attempts = []

    def blocked(video_id):
        attempts.append(video_id)
        raise IpBlocked("blocked")

    result = collect_and_upload(
        channels=[{"name": "채널A"}],
        service_url="https://sector.example",
        token="secret",
        now=datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc),
        lookback_hours=36,
        known_video_ids=set(),
        fetch_videos=lambda _channel: videos,
        fetch_script=blocked,
        upload=lambda *_args: None,
    )

    assert attempts == ["one"]
    assert result["ip_blocked"] is True


def test_collector_waits_between_transcript_requests():
    videos = [
        {"video_id": "one", "title": "1", "published_at": "2026-08-24T00:00:00+00:00", "url": "https://youtu.be/one"},
        {"video_id": "two", "title": "2", "published_at": "2026-08-24T00:00:00+00:00", "url": "https://youtu.be/two"},
    ]
    sleeps = []

    result = collect_and_upload(
        channels=[{"name": "채널A"}],
        service_url="https://sector.example",
        token="secret",
        now=datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc),
        lookback_hours=36,
        known_video_ids=set(),
        fetch_videos=lambda _channel: videos,
        fetch_script=lambda _video_id: "다음 거래일 반도체 전망을 설명하는 충분히 긴 자막입니다.",
        upload=lambda *_args: None,
        delay_seconds=12,
        sleeper=sleeps.append,
    )

    assert result["videos_uploaded"] == 2
    assert sleeps == [12]


def test_cli_numbers_reject_non_finite_or_invalid_values():
    for invalid_delay in (float("nan"), float("inf"), float("-inf"), -1):
        with pytest.raises(ValueError):
            validate_cli_numbers(hours=36, max_per_channel=1, batch_size=5, delay=invalid_delay)

    validate_cli_numbers(hours=36, max_per_channel=1, batch_size=5, delay=0)


def test_batch_state_round_trip_is_atomic(tmp_path):
    state = tmp_path / ".collector-state.json"

    save_batch_start(state, 10)

    assert load_batch_start(state) == 10
    assert not (tmp_path / ".collector-state.json.tmp").exists()
