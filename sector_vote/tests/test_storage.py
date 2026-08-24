from sector_vote.storage import SectorStore


def test_store_replaces_calls_when_same_video_is_reanalysed(tmp_path):
    store = SectorStore(tmp_path / "sector.db")
    store.save_video_calls(
        video_id="abc",
        channel="채널A",
        title="오전 전망",
        url="https://youtube.com/watch?v=abc",
        published_at="2026-08-24T08:00:00+09:00",
        calls=[{
            "sector": "반도체",
            "direction": "up",
            "horizon": "next_session",
            "confidence": 0.8,
            "reason": "수요 회복",
            "quote": "반도체가 강할 수 있습니다",
        }],
    )
    store.save_video_calls(
        video_id="abc",
        channel="채널A",
        title="오전 전망",
        url="https://youtube.com/watch?v=abc",
        published_at="2026-08-24T08:00:00+09:00",
        calls=[{
            "sector": "반도체",
            "direction": "down",
            "horizon": "next_session",
            "confidence": 0.7,
            "reason": "차익 실현",
            "quote": "오늘은 쉬어갈 수 있습니다",
        }],
    )

    rows = store.list_calls()

    assert len(rows) == 1
    assert rows[0]["direction"] == "down"
    assert rows[0]["channel"] == "채널A"


def test_store_normalizes_published_time_to_utc(tmp_path):
    store = SectorStore(tmp_path / "sector.db")
    store.save_video_calls(
        video_id="offset",
        channel="채널A",
        title="전망",
        url="https://youtube.com/watch?v=offset",
        published_at="2026-08-24T08:00:00+09:00",
        calls=[],
    )

    with store._con() as con:
        published_at = con.execute(
            "SELECT published_at FROM videos WHERE video_id='offset'"
        ).fetchone()[0]

    assert published_at == "2026-08-23T23:00:00+00:00"
