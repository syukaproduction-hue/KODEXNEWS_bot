import time
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from sector_vote.app import create_app


def _classifier(_transcript):
    return [{
        "sector": "반도체", "direction": "up", "horizon": "next_session",
        "confidence": 0.9, "reason": "수요 회복", "quote": "반도체가 강할 수 있습니다",
    }]


def test_manual_ingest_builds_sector_only_public_summary(tmp_path):
    app = create_app(
        tmp_path / "sector.db",
        admin_token="secret",
        classify_fn=_classifier,
        now_fn=lambda: datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc),
    )
    client = TestClient(app)

    unauthorized = client.post("/api/ingest/transcript", json={})
    assert unauthorized.status_code == 401

    response = client.post(
        "/api/ingest/transcript",
        headers={"X-Admin-Token": "secret"},
        json={
            "video_id": "abc",
            "channel": "  테스트 채널  ",
            "title": "내일 전망",
            "url": "https://youtube.com/watch?v=abc",
            "published_at": "2026-08-24T08:00:00+09:00",
            "transcript": "내일 반도체 섹터는 메모리 수요 회복으로 상대적으로 강할 수 있습니다.",
        },
    )
    assert response.status_code == 200

    summary = client.get("/api/summary").json()
    assert summary["sectors"][0]["sector"] == "반도체"
    evidence = summary["sectors"][0]["evidence"][0]
    assert evidence["channel"] == "테스트 채널"
    assert evidence["quote"] == "반도체가 강할 수 있습니다"
    assert evidence["video_url"] == "https://www.youtube.com/watch?v=abc"
    assert evidence["thumbnail_url"] == "https://i.ytimg.com/vi/abc/mqdefault.jpg"

    page = client.get("/")
    assert page.status_code == 200
    assert "내일 섹터 한표" in page.text
    assert "테스트 채널" in page.text
    assert "반도체가 강할 수 있습니다" in page.text
    assert "https://www.youtube.com/watch?v=abc" in page.text
    assert "https://i.ytimg.com/vi/abc/mqdefault.jpg" in page.text
    assert "근거 영상 1개" in page.text

    assert client.get("/api/evidence").status_code == 401
    evidence = client.get("/api/evidence", headers={"X-Admin-Token": "secret"}).json()
    assert evidence[0]["channel"] == "테스트 채널"
    assert "transcript" not in evidence[0]


def test_ingest_rejects_blank_channel_after_trimming(tmp_path):
    client = TestClient(create_app(tmp_path / "sector.db", admin_token="secret", classify_fn=_classifier))
    response = client.post("/api/ingest/transcript", headers={"X-Admin-Token": "secret"}, json={
        "video_id": "blank",
        "channel": "   ",
        "title": "영상",
        "url": "https://youtube.com/watch?v=blank",
        "published_at": datetime.now(timezone.utc).isoformat(),
        "transcript": "다음 거래일 섹터 전망을 설명하는 충분히 긴 진단용 스크립트입니다.",
    })

    assert response.status_code == 422


def test_public_evidence_labels_reason_when_quote_is_empty(tmp_path):
    def classifier(_text):
        return [{
            "sector": "금융", "direction": "up", "horizon": "next_session",
            "confidence": 0.8, "reason": "금리 환경이 금융 섹터에 우호적이라는 AI 요약입니다.", "quote": "",
        }]

    client = TestClient(create_app(tmp_path / "sector.db", admin_token="secret", classify_fn=classifier))
    client.post("/api/ingest/transcript", headers={"X-Admin-Token": "secret"}, json={
        "video_id": "summary",
        "channel": "채널A",
        "title": "영상",
        "url": "https://youtube.com/watch?v=summary",
        "published_at": datetime.now(timezone.utc).isoformat(),
        "transcript": "다음 거래일 금융 섹터 전망을 설명하는 충분히 긴 진단용 스크립트입니다.",
    })

    page = client.get("/").text

    assert "판정 요약" in page
    assert "“금리 환경이 금융 섹터에 우호적이라는 AI 요약입니다.”" not in page


def test_public_evidence_escapes_channel_title_and_quote(tmp_path):
    def classifier(_text):
        return [{
            "sector": "금융", "direction": "up", "horizon": "next_session",
            "confidence": 0.8, "reason": "<b>이유</b>", "quote": "<script>alert(1)</script>",
        }]

    app = create_app(tmp_path / "sector.db", admin_token="secret", classify_fn=classifier)
    client = TestClient(app)
    client.post("/api/ingest/transcript", headers={"X-Admin-Token": "secret"}, json={
        "video_id": "safe_id",
        "channel": "<img src=x onerror=alert(1)>",
        "title": "<b>영상</b>",
        "url": "https://youtube.com/watch?v=safe_id",
        "published_at": datetime.now(timezone.utc).isoformat(),
        "transcript": "다음 거래일 금융 섹터가 강할 수 있다는 충분히 긴 진단용 스크립트입니다.",
    })

    page = client.get("/").text

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<img src=x" not in page
    assert "&lt;img src=x onerror=alert(1)&gt;" in page


def test_processed_videos_endpoint_includes_zero_call_video(tmp_path):
    app = create_app(tmp_path / "sector.db", admin_token="secret", classify_fn=lambda _text: [])
    client = TestClient(app)
    headers = {"X-Admin-Token": "secret"}
    client.post("/api/ingest/transcript", headers=headers, json={
        "video_id": "empty",
        "channel": "채널A",
        "title": "방향 없는 영상",
        "url": "https://youtube.com/watch?v=empty",
        "published_at": "2026-08-24T08:00:00+09:00",
        "transcript": "이 영상에는 다음 거래일 섹터 방향을 특정하는 내용이 없습니다.",
    })

    assert client.get("/api/videos").status_code == 401
    response = client.get("/api/videos", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"video_ids": ["empty"]}


def test_health_reports_ready(tmp_path):
    client = TestClient(create_app(tmp_path / "sector.db", admin_token="secret", classify_fn=_classifier))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["automation"] == {
        "enabled": False,
        "interval_minutes": 0,
        "provider": "youtube-direct",
    }


def test_automatic_refresh_runs_on_service_startup(tmp_path):
    video = {
        "video_id": "auto", "title": "자동 전망",
        "published_at": "2099-01-01T00:00:00+00:00",
        "url": "https://youtube.com/watch?v=auto",
    }
    app = create_app(
        tmp_path / "sector.db",
        admin_token="secret",
        classify_fn=_classifier,
        channels=[{"name": "채널A", "channel_id": "a"}],
        fetch_videos_fn=lambda _channel: [video],
        fetch_script_fn=lambda _video_id: "내일 반도체 섹터가 강할 수 있다는 충분히 긴 자동 자막입니다.",
        auto_refresh_minutes=60,
        transcript_provider_name="supadata",
    )

    with TestClient(app) as client:
        deadline = time.monotonic() + 2
        status = {}
        while time.monotonic() < deadline:
            status = client.get("/api/refresh/status", headers={"X-Admin-Token": "secret"}).json()
            if status.get("last_result"):
                break
            time.sleep(0.01)

        assert status["last_result"]["videos_analyzed"] == 1
        assert client.get("/health").json()["automation"] == {
            "enabled": True,
            "interval_minutes": 60,
            "provider": "supadata",
        }


def test_supadata_key_enables_six_hour_server_automation_by_default(tmp_path, monkeypatch):
    transcript_requests = []
    monkeypatch.setenv("SUPADATA_API_KEY", "provider-secret")
    monkeypatch.delenv("SECTOR_AUTO_REFRESH_MINUTES", raising=False)
    monkeypatch.setattr(
        "sector_vote.app.fetch_supadata_transcript",
        lambda video_id, api_key, **_kwargs: transcript_requests.append((video_id, api_key)) or "내일 금융 섹터가 강할 수 있습니다.",
    )
    video = {
        "video_id": "managed", "title": "자동 전망",
        "published_at": "2099-01-01T00:00:00+00:00",
        "url": "https://youtube.com/watch?v=managed",
    }
    app = create_app(
        tmp_path / "sector.db",
        admin_token="secret",
        classify_fn=_classifier,
        channels=[{"name": "채널A", "channel_id": "a"}],
        fetch_videos_fn=lambda _channel: [video],
    )

    with TestClient(app) as client:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not transcript_requests:
            time.sleep(0.01)

        assert transcript_requests == [("managed", "provider-secret")]
        assert client.get("/health").json()["automation"] == {
            "enabled": True,
            "interval_minutes": 360,
            "provider": "supadata",
        }


def test_automatic_refresh_rejects_non_finite_interval(tmp_path):
    for value in (float("nan"), float("inf"), float("-inf"), -1):
        try:
            create_app(tmp_path / f"{value}.db", admin_token="secret", auto_refresh_minutes=value)
        except ValueError:
            continue
        raise AssertionError(f"accepted invalid interval: {value}")


def test_refresh_endpoint_runs_configured_channel_pipeline(tmp_path):
    video = {
        "video_id": "new", "title": "내일 전망",
        "published_at": "2099-01-01T00:00:00+00:00",
        "url": "https://youtube.com/watch?v=new",
    }
    app = create_app(
        tmp_path / "sector.db",
        admin_token="secret",
        classify_fn=_classifier,
        channels=[{"name": "채널A", "channel_id": "a"}],
        fetch_videos_fn=lambda _channel: [video],
        fetch_script_fn=lambda _video_id: "내일 반도체 섹터는 메모리 수요 회복으로 상대적으로 강할 수 있습니다.",
    )
    client = TestClient(app)

    response = client.post("/api/refresh", headers={"X-Admin-Token": "secret"})

    assert response.status_code == 202
    assert client.get("/api/refresh/status").status_code == 401
    status = client.get("/api/refresh/status", headers={"X-Admin-Token": "secret"}).json()
    assert status["last_result"]["videos_analyzed"] == 1


def test_summary_excludes_calls_older_than_daily_window(tmp_path):
    def classifier(transcript):
        sector = "2차전지" if "배터리" in transcript else "반도체"
        return [{
            "sector": sector, "direction": "up", "horizon": "next_session",
            "confidence": 0.8, "reason": "테스트", "quote": transcript,
        }]

    app = create_app(
        tmp_path / "sector.db",
        admin_token="secret",
        classify_fn=classifier,
        now_fn=lambda: datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc),
    )
    client = TestClient(app)
    headers = {"X-Admin-Token": "secret"}
    base = {
        "channel": "채널A", "title": "전망", "url": "https://youtube.com/watch?v=x",
    }
    client.post("/api/ingest/transcript", headers=headers, json={
        **base, "video_id": "old", "published_at": "2026-08-20T00:00:00+00:00",
        "transcript": "배터리 섹터가 내일 강할 수 있다는 오래된 전망입니다.",
    })
    client.post("/api/ingest/transcript", headers=headers, json={
        **base, "video_id": "new", "published_at": "2026-08-24T00:00:00+00:00",
        "transcript": "반도체 섹터가 내일 강할 수 있다는 새로운 전망입니다.",
    })

    summary = client.get("/api/summary").json()
    sectors = summary["sectors"]

    assert [row["sector"] for row in sectors] == ["반도체"]
    assert {row["sector"] for row in summary["weekly_sectors"]} == {"반도체", "2차전지"}
    assert "최근 7일 관심 섹터" in client.get("/").text
