from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

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
    assert store.list_video_ids() == {"offset"}


def test_video_claim_is_atomic_and_respects_retry_window(tmp_path):
    store = SectorStore(tmp_path / "sector.db")
    now = datetime(2026, 9, 4, 0, 0, tzinfo=timezone.utc)

    assert store.claim_video("video", now) is True
    assert store.claim_video("video", now) is False
    assert store.claim_video("video", now + timedelta(minutes=31)) is False

    store.mark_video_attempt("video", status="failed", now=now, retry_minutes=60, error="temporary")
    assert store.claim_video("video", now + timedelta(minutes=59)) is False
    assert store.claim_video("video", now + timedelta(minutes=61)) is True


def test_unavailable_video_is_not_claimed_again(tmp_path):
    store = SectorStore(tmp_path / "sector.db")
    now = datetime(2026, 9, 4, 0, 0, tzinfo=timezone.utc)
    assert store.claim_video("video", now) is True

    store.mark_video_attempt("video", status="unavailable", now=now, error="no captions")

    assert store.claim_video("video", now + timedelta(days=30)) is False


def test_two_store_instances_cannot_claim_same_video(tmp_path):
    path = tmp_path / "sector.db"
    first = SectorStore(path)
    second = SectorStore(path)
    now = datetime(2026, 9, 4, 0, 0, tzinfo=timezone.utc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda store: store.claim_video("shared", now), [first, second]))

    assert sorted(results) == [False, True]


def test_prune_before_removes_old_metadata_and_calls(tmp_path):
    store = SectorStore(tmp_path / "sector.db")
    for video_id, published_at in (
        ("old", "2026-07-01T00:00:00+00:00"),
        ("new", "2026-09-01T00:00:00+00:00"),
    ):
        store.save_video_calls(
            video_id=video_id,
            channel="채널",
            title="전망",
            url=f"https://youtu.be/{video_id}",
            published_at=published_at,
            calls=[{
                "sector": "금융", "direction": "up", "horizon": "next_session",
                "confidence": 0.8, "reason": "이유", "quote": "인용",
            }],
        )

    removed = store.prune_before("2026-08-05T00:00:00+00:00")

    assert removed == 1
    assert store.list_video_ids() == {"new"}
    assert [row["video_id"] for row in store.list_calls()] == ["new"]
