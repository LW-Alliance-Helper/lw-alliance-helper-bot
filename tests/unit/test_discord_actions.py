"""Tests for the Phase 8 Discord-action endpoints (#316): channels + post-image.

The gateway is faked (no real Discord); for post-image the channel is a
``MagicMock(spec=discord.TextChannel)`` so the handler's isinstance check passes,
and ``channel.send`` is an AsyncMock returning a message with ``jump_url``.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from aiohttp.test_utils import TestClient, TestServer

from api_server import build_app
from tests.conftest import TEST_GUILD_ID

AUTH = {"Authorization": "Bearer testkey"}
PNG_B64 = base64.b64encode(b"fake-png-bytes").decode()


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("MAPMANAGER_API_KEY", "testkey")


def _perms(send=True, attach=True):
    return SimpleNamespace(send_messages=send, attach_files=attach)


def _text_channel(name, send=True, attach=True):
    return SimpleNamespace(
        id=int(abs(hash(name)) % 10_000_000),
        name=name,
        permissions_for=lambda me, p=_perms(send, attach): p,
    )


# ── channels ──────────────────────────────────────────────────────────────────


async def test_channels_requires_auth():
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(f"/api/guilds/{TEST_GUILD_ID}/channels")
        assert r.status == 401


async def test_channels_empty_when_bot_not_in_guild():
    bot = SimpleNamespace(get_guild=lambda gid: None)
    async with TestClient(TestServer(build_app(bot=bot))) as client:
        r = await client.get(f"/api/guilds/{TEST_GUILD_ID}/channels", headers=AUTH)
        assert r.status == 200
        assert (await r.json()) == {"channels": []}


async def test_channels_filters_by_permission_and_keeps_order():
    channels = [
        _text_channel("storm-planning", send=True, attach=True),
        _text_channel("no-attach", send=True, attach=False),
        _text_channel("read-only", send=False, attach=True),
        _text_channel("general", send=True, attach=True),
    ]
    guild = SimpleNamespace(me=object(), text_channels=channels)
    bot = SimpleNamespace(get_guild=lambda gid: guild)
    async with TestClient(TestServer(build_app(bot=bot))) as client:
        r = await client.get(f"/api/guilds/{TEST_GUILD_ID}/channels", headers=AUTH)
        body = await r.json()
    names = [c["name"] for c in body["channels"]]
    assert names == ["storm-planning", "general"]  # only postable, in order
    assert all(isinstance(c["id"], str) for c in body["channels"])


# ── post-image ────────────────────────────────────────────────────────────────


def _text_channel_mock(send=True, attach=True, jump="https://discord.com/channels/1/2/3"):
    ch = MagicMock(spec=discord.TextChannel)
    ch.permissions_for.return_value = _perms(send, attach)
    ch.send = AsyncMock(return_value=SimpleNamespace(jump_url=jump))
    return ch


def _guild_with_channel(channel):
    return SimpleNamespace(me=object(), get_channel=lambda cid: channel)


def _body(**over):
    b = {"channel_id": "555", "filename": "DS.png", "image_base64": PNG_B64, "message": "cap"}
    b.update(over)
    return b


class _Resp:
    """Holds status + parsed body so tests can read them after the TestClient
    context closes (the connection is gone by then)."""

    def __init__(self, status, data):
        self.status = status
        self._data = data

    async def json(self):
        return self._data


async def _post(bot, body):
    async with TestClient(TestServer(build_app(bot=bot))) as client:
        r = await client.post(f"/api/guilds/{TEST_GUILD_ID}/post-image", headers=AUTH, json=body)
        return _Resp(r.status, await r.json())


async def test_post_image_requires_auth():
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.post(f"/api/guilds/{TEST_GUILD_ID}/post-image", json=_body())
        assert r.status == 401


async def test_post_image_success():
    channel = _text_channel_mock()
    bot = SimpleNamespace(get_guild=lambda gid: _guild_with_channel(channel))
    r = await _post(bot, _body())
    assert r.status == 200
    assert (await r.json()) == {
        "posted": True,
        "message_url": "https://discord.com/channels/1/2/3",
    }
    # caption + file forwarded to Discord
    _args, kwargs = channel.send.call_args
    assert kwargs["content"] == "cap"
    assert isinstance(kwargs["file"], discord.File)


async def test_post_image_missing_image_400():
    bot = SimpleNamespace(get_guild=lambda gid: _guild_with_channel(_text_channel_mock()))
    r = await _post(bot, _body(image_base64=""))
    assert r.status == 400
    assert (await r.json())["error"] == "missing_image"


async def test_post_image_bad_base64_400():
    bot = SimpleNamespace(get_guild=lambda gid: _guild_with_channel(_text_channel_mock()))
    r = await _post(bot, _body(image_base64="!!not base64!!"))
    assert r.status == 400
    assert (await r.json())["error"] == "bad_base64"


async def test_post_image_bad_channel_id_400():
    bot = SimpleNamespace(get_guild=lambda gid: _guild_with_channel(_text_channel_mock()))
    r = await _post(bot, _body(channel_id="not-an-int"))
    assert r.status == 400


async def test_post_image_guild_not_found_404():
    bot = SimpleNamespace(get_guild=lambda gid: None)
    r = await _post(bot, _body())
    assert r.status == 404
    assert (await r.json())["error"] == "guild_not_found"


async def test_post_image_channel_not_found_404():
    bot = SimpleNamespace(get_guild=lambda gid: _guild_with_channel(None))
    r = await _post(bot, _body())
    assert r.status == 404
    assert (await r.json())["error"] == "channel_not_found"


async def test_post_image_missing_permission_403():
    channel = _text_channel_mock(attach=False)
    bot = SimpleNamespace(get_guild=lambda gid: _guild_with_channel(channel))
    r = await _post(bot, _body())
    assert r.status == 403
    assert (await r.json())["error"] == "missing_permission"


async def test_post_image_discord_error_400():
    channel = _text_channel_mock()
    channel.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=413), "too big"))
    bot = SimpleNamespace(get_guild=lambda gid: _guild_with_channel(channel))
    r = await _post(bot, _body())
    assert r.status == 400
    assert "discord_error" in (await r.json())["error"]
