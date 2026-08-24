from sector_vote.sector_logic import SectorCall, aggregate_calls, normalize_sector


def test_normalize_sector_maps_common_aliases():
    assert normalize_sector("HBM 반도체") == "반도체"
    assert normalize_sector("방산주") == "조선·방산"
    assert normalize_sector("배터리") == "2차전지"


def test_aggregate_counts_each_channel_once_per_sector():
    calls = [
        SectorCall("채널A", "반도체", "up", "next_session", "2026-08-24T08:00:00+09:00"),
        SectorCall("채널A", "반도체", "up", "next_session", "2026-08-24T09:00:00+09:00"),
        SectorCall("채널B", "반도체", "down", "next_session", "2026-08-24T08:30:00+09:00"),
    ]

    result = aggregate_calls(calls)

    assert result[0]["sector"] == "반도체"
    assert result[0]["up"] == 1
    assert result[0]["down"] == 1
    assert result[0]["neutral"] == 0
    assert result[0]["consensus"] == "neutral"
    assert "channel" not in result[0]


def test_aggregate_uses_last_call_when_same_video_has_conflicting_rows():
    calls = [
        SectorCall("채널A", "반도체", "up", "next_session", "2026-08-24T08:00:00+09:00"),
        SectorCall("채널A", "반도체", "down", "next_session", "2026-08-24T08:00:00+09:00"),
    ]

    result = aggregate_calls(calls)

    assert result[0]["up"] == 0
    assert result[0]["down"] == 1
    assert result[0]["consensus"] == "down"


def test_aggregate_excludes_non_next_session_horizons():
    calls = [
        SectorCall("채널A", "AI", "up", "long_term", "2026-08-24T08:00:00+09:00"),
        SectorCall("채널B", "AI", "up", "next_session", "2026-08-24T08:30:00+09:00"),
    ]

    result = aggregate_calls(calls)

    assert result == [{
        "sector": "AI·로봇",
        "up": 1,
        "down": 0,
        "neutral": 0,
        "total": 1,
        "consensus": "up",
    }]
