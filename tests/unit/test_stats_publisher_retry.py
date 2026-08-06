"""Stats publisher retry behaviour (#382 / #383).

GitHub 503s on the odd request. The publisher used to report the first one as
an error, which auto-filed a GitHub issue for something that would have worked
on a second try. These pin down that a blip is retried and stays silent, while
a genuinely stuck publish still reports.

Backoff sleeps are patched out; nothing here should take wall-clock time.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import aiohttp
import pytest

import stats_publisher


class _FakeResponse:
    def __init__(self, status: int, payload=None, text: str = ""):
        self.status = status
        self._payload = payload
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):
        return self._text

    async def json(self):
        return self._payload


class _FakeSession:
    """Plays back a scripted list of responses and records what was asked.

    An entry may also be an exception instance, which is raised instead, so a
    connection error mid-sequence is expressible.
    """

    def __init__(self, get_results=None, put_results=None):
        self._get = list(get_results or [])
        self._put = list(put_results or [])
        self.get_calls = []
        self.put_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _next(self, queue, label):
        if not queue:
            raise AssertionError(f"unexpected extra {label} request")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self._next(self._get, "GET")

    def put(self, url, **kwargs):
        self.put_calls.append((url, kwargs))
        return self._next(self._put, "PUT")


def _contents_payload(alliances: int, sha: str = "sha123") -> dict:
    """The shape GitHub's Contents API returns: base64 content plus a sha."""
    import base64

    raw = json.dumps({"alliances": alliances, "updated_utc": "2026-07-20T00:00:00+00:00"})
    return {"sha": sha, "content": base64.b64encode(raw.encode()).decode()}


@pytest.fixture(autouse=True)
def _no_backoff_sleep():
    """Keep the suite fast without weakening the retry assertions."""
    with patch("stats_publisher.asyncio.sleep", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _token_set(monkeypatch):
    monkeypatch.setenv("STATS_GITHUB_TOKEN", "test-token")


@pytest.fixture
def captures():
    """Collect anything the publisher would have sent to Sentry."""
    sent = []
    with patch("stats_publisher._capture_publish_failure", side_effect=sent.append):
        yield sent


class TestPutRetry:
    @pytest.mark.asyncio
    async def test_503_then_success_is_silent(self, captures):
        """The exact #382 / #383 shape: one blip, then it works."""
        session = _FakeSession(
            put_results=[_FakeResponse(503, text="no server"), _FakeResponse(200)]
        )
        ok = await stats_publisher._put_new(session, "t", {"alliances": 5}, "sha123", "msg")
        assert ok is True
        assert len(session.put_calls) == 2
        assert captures == []

    @pytest.mark.asyncio
    async def test_persistent_503_reports_once_after_retries(self, captures):
        """Still worth knowing about if GitHub is down across the whole backoff."""
        session = _FakeSession(put_results=[_FakeResponse(503, text="no server")] * 3)
        ok = await stats_publisher._put_new(session, "t", {"alliances": 5}, "sha123", "msg")
        assert ok is False
        assert len(session.put_calls) == 3
        assert len(captures) == 1
        assert "503" in captures[0] and "3 attempt(s)" in captures[0]

    @pytest.mark.asyncio
    async def test_401_reports_immediately_without_retrying(self, captures):
        """An expired PAT will not fix itself; retrying only delays the alert."""
        session = _FakeSession(put_results=[_FakeResponse(401, text="Bad credentials")])
        ok = await stats_publisher._put_new(session, "t", {"alliances": 5}, "sha123", "msg")
        assert ok is False
        assert len(session.put_calls) == 1
        assert len(captures) == 1
        assert "401" in captures[0]

    @pytest.mark.asyncio
    async def test_422_neither_retries_nor_reports(self, captures):
        """A sha conflict is our own bug to fix, but not a Sentry-worthy one and
        certainly not retryable with the same body."""
        session = _FakeSession(put_results=[_FakeResponse(422, text="sha mismatch")])
        ok = await stats_publisher._put_new(session, "t", {"alliances": 5}, None, "msg")
        assert ok is False
        assert len(session.put_calls) == 1
        assert captures == []

    @pytest.mark.asyncio
    async def test_connection_error_is_retried(self, captures):
        session = _FakeSession(
            put_results=[aiohttp.ClientError("reset"), _FakeResponse(201)],
        )
        ok = await stats_publisher._put_new(session, "t", {"alliances": 5}, "sha123", "msg")
        assert ok is True
        assert len(session.put_calls) == 2
        assert captures == []


class TestFetchRetry:
    @pytest.mark.asyncio
    async def test_503_then_success(self):
        session = _FakeSession(
            get_results=[
                _FakeResponse(503, text="no server"),
                _FakeResponse(200, _contents_payload(7)),
            ]
        )
        existing, sha = await stats_publisher._fetch_current(session, "t")
        assert existing == {"alliances": 7, "updated_utc": "2026-07-20T00:00:00+00:00"}
        assert sha == "sha123"
        assert len(session.get_calls) == 2

    @pytest.mark.asyncio
    async def test_404_means_absent_not_unknown(self):
        """A missing file is a legitimate first publish, so the caller should
        go ahead with a sha-less PUT."""
        session = _FakeSession(get_results=[_FakeResponse(404)])
        existing, sha = await stats_publisher._fetch_current(session, "t")
        assert existing is None
        assert sha is None
        assert len(session.get_calls) == 1

    @pytest.mark.asyncio
    async def test_persistent_failure_is_unknown(self):
        session = _FakeSession(get_results=[_FakeResponse(503, text="no server")] * 3)
        existing, sha = await stats_publisher._fetch_current(session, "t")
        assert existing is stats_publisher.UNKNOWN
        assert sha is None
        assert len(session.get_calls) == 3

    @pytest.mark.asyncio
    async def test_malformed_body_keeps_the_sha_and_overwrites(self):
        """The file is there and its sha is good; only the content is junk."""
        session = _FakeSession(
            get_results=[_FakeResponse(200, {"sha": "sha999", "content": "not-base64!!"})]
        )
        existing, sha = await stats_publisher._fetch_current(session, "t")
        assert existing is None
        assert sha == "sha999"


class TestPublishAllianceCount:
    async def _run(self, session):
        with patch("stats_publisher.aiohttp.ClientSession", return_value=session):
            await stats_publisher.publish_alliance_count(9)
        return session

    @pytest.mark.asyncio
    async def test_unreadable_current_file_skips_the_write(self, captures):
        """Without a sha, a PUT over an existing file is a guaranteed 422, so
        the run is skipped rather than spent."""
        session = _FakeSession(get_results=[_FakeResponse(503, text="down")] * 3, put_results=[])
        await self._run(session)
        assert session.put_calls == []
        assert captures == []

    @pytest.mark.asyncio
    async def test_unchanged_count_does_not_write(self, captures):
        session = _FakeSession(get_results=[_FakeResponse(200, _contents_payload(9))])
        await self._run(session)
        assert session.put_calls == []

    @pytest.mark.asyncio
    async def test_changed_count_writes_with_the_sha(self, captures):
        session = _FakeSession(
            get_results=[_FakeResponse(200, _contents_payload(4))],
            put_results=[_FakeResponse(200)],
        )
        await self._run(session)
        assert len(session.put_calls) == 1
        body = session.put_calls[0][1]["json"]
        assert body["sha"] == "sha123"
        assert captures == []

    @pytest.mark.asyncio
    async def test_first_ever_publish_sends_no_sha(self, captures):
        session = _FakeSession(
            get_results=[_FakeResponse(404)],
            put_results=[_FakeResponse(201)],
        )
        await self._run(session)
        assert len(session.put_calls) == 1
        assert "sha" not in session.put_calls[0][1]["json"]


if __name__ == "__main__":
    pytest.main([__file__])
