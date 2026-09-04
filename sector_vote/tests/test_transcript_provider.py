import pytest

from sector_vote.transcript_provider import (
    ProviderAccessError,
    ProviderLimitExceeded,
    ProviderProtocolError,
    TranscriptUnavailable,
    fetch_supadata_transcript,
)


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def test_supadata_fetches_native_korean_transcript_without_redirects():
    session = FakeSession([FakeResponse(200, {"content": "첫 문장 두 번째 문장", "lang": "ko"})])

    transcript = fetch_supadata_transcript("abc_123-XYZ", "api-secret", session=session)

    assert transcript == "첫 문장 두 번째 문장"
    url, kwargs = session.calls[0]
    assert url == "https://api.supadata.ai/v1/transcript"
    assert kwargs["headers"] == {"x-api-key": "api-secret"}
    assert kwargs["params"] == {
        "url": "https://www.youtube.com/watch?v=abc_123-XYZ",
        "lang": "ko",
        "text": "true",
        "mode": "native",
    }
    assert kwargs["allow_redirects"] is False
    assert kwargs["timeout"] == 45


def test_supadata_joins_timestamped_chunks_when_returned():
    session = FakeSession([FakeResponse(200, {
        "content": [{"text": "첫 문장"}, {"text": "두 번째 문장"}],
        "lang": "ko",
    })])

    transcript = fetch_supadata_transcript("video123", "key", session=session)

    assert transcript == "첫 문장 두 번째 문장"


def test_supadata_polls_asynchronous_job_until_completed():
    session = FakeSession([
        FakeResponse(202, {"jobId": "job-123"}),
        FakeResponse(200, {"status": "active"}),
        FakeResponse(200, {"status": "completed", "content": "완료된 자막"}),
    ])
    sleeps = []

    transcript = fetch_supadata_transcript(
        "video123",
        "key",
        session=session,
        sleeper=sleeps.append,
        max_polls=3,
    )

    assert transcript == "완료된 자막"
    assert sleeps == [1]
    assert session.calls[1][0] == "https://api.supadata.ai/v1/transcript/job-123"
    assert session.calls[1][1]["allow_redirects"] is False


def test_supadata_reports_unavailable_without_retrying_forever():
    session = FakeSession([FakeResponse(206, {"error": "transcript-unavailable"})])

    with pytest.raises(TranscriptUnavailable):
        fetch_supadata_transcript("video123", "key", session=session)


def test_supadata_reports_rate_or_credit_limit():
    session = FakeSession([FakeResponse(429, {"error": "limit-exceeded"})])

    with pytest.raises(ProviderLimitExceeded):
        fetch_supadata_transcript("video123", "key", session=session)


def test_supadata_reports_invalid_or_forbidden_key():
    for status_code in (401, 402, 403):
        session = FakeSession([FakeResponse(status_code, {"error": "unauthorized"})])
        with pytest.raises(ProviderAccessError):
            fetch_supadata_transcript("video123", "bad-key", session=session)


def test_supadata_rejects_redirect_without_following_it():
    session = FakeSession([FakeResponse(302, {})])

    with pytest.raises(ProviderProtocolError):
        fetch_supadata_transcript("video123", "key", session=session)


def test_supadata_stops_when_polling_hits_limit():
    session = FakeSession([
        FakeResponse(202, {"jobId": "job-123"}),
        FakeResponse(429, {"error": "limit-exceeded"}),
    ])

    with pytest.raises(ProviderLimitExceeded):
        fetch_supadata_transcript("video123", "key", session=session)


def test_supadata_stops_when_polling_requires_plan_upgrade():
    session = FakeSession([
        FakeResponse(202, {"jobId": "job-123"}),
        FakeResponse(402, {"error": "upgrade-required"}),
    ])

    with pytest.raises(ProviderAccessError):
        fetch_supadata_transcript("video123", "key", session=session)


def test_supadata_preserves_nested_failed_job_message():
    session = FakeSession([
        FakeResponse(202, {"jobId": "job-123"}),
        FakeResponse(200, {
            "status": "failed",
            "error": {"code": "transcript-unavailable", "message": "No transcript"},
        }),
    ])

    with pytest.raises(TranscriptUnavailable, match="transcript-unavailable: No transcript"):
        fetch_supadata_transcript("video123", "key", session=session)


def test_supadata_accepts_completed_job_result_wrapper():
    session = FakeSession([
        FakeResponse(202, {"jobId": "job-123"}),
        FakeResponse(200, {
            "status": "completed",
            "result": {"content": "래핑된 자막", "lang": "ko"},
        }),
    ])

    assert fetch_supadata_transcript("video123", "key", session=session) == "래핑된 자막"


def test_supadata_polling_can_be_cancelled_during_shutdown():
    class CancelEvent:
        def wait(self, seconds):
            assert seconds == 1
            return True

    session = FakeSession([
        FakeResponse(202, {"jobId": "job-123"}),
        FakeResponse(200, {"status": "active"}),
    ])

    with pytest.raises(InterruptedError):
        fetch_supadata_transcript(
            "video123", "key", session=session, cancel_event=CancelEvent(), max_polls=3
        )


def test_supadata_rejects_missing_key_and_malformed_video_id():
    with pytest.raises(ValueError):
        fetch_supadata_transcript("video123", "", session=FakeSession([]))
    with pytest.raises(ValueError):
        fetch_supadata_transcript("../../secret", "key", session=FakeSession([]))
