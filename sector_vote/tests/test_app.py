from datetime import datetime, timezone

from fastapi.testclient import TestClient

from sector_vote.app import create_app


def _classifier(_transcript):
    return [{
        "sector": "반도체", "direction": "up", "horizon": "next_session",
        "confidence": 0.9, "reason": "수요 회복", "quote": "반도체가 강할 수 있습니다",
    }]


def test_manual_ingest_builds_sector_only_public_summary(tmp_path):
    app = create_app(tmp_path / "sector.db", admin_token="secret", classify_fn=_classifier)
    client = TestClient(app)

    unauthorized = client.post("/api/ingest/transcript", json={})
    assert unauthorized.status_code == 401

    response = client.post(
        "/api/ingest/transcript",
        headers={"X-Admin-Token": "secret"},
        json={
            "video_id": "abc",
            "channel": "테스트 채널",
            "title": "내일 전망",
            "url": "https://youtube.com/watch?v=abc",
            "published_at": "2026-08-24T08:00:00+09:00",
            "transcript": "내일 반도체 섹터는 메모리 수요 회복으로 상대적으로 강할 수 있습니다.",
        },
    )
    assert response.status_code == 200

    summary = client.get("/api/summary").json()
    assert summary["sectors"][0]["sector"] == "반도체"
    assert "channel" not in summary["sectors"][0]

    page = client.get("/")
    assert page.status_code == 200
    assert "내일 섹터 한표" in page.text
    assert "테스트 채널" not in page.text

    assert client.get("/api/evidence").status_code == 401
    evidence = client.get("/api/evidence", headers={"X-Admin-Token": "secret"}).json()
    assert evidence[0]["channel"] == "테스트 채널"
    assert "transcript" not in evidence[0]


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

    sectors = client.get("/api/summary").json()["sectors"]

    assert [row["sector"] for row in sectors] == ["반도체"]
