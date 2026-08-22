"""Sentry is initialised without stack-frame locals (#518).

privacy.html tells users that crash reports "do not include Discord user IDs,
member names, or the contents of your Google Sheet". Only
``include_local_variables=False`` makes that sentence true.
``send_default_pii=False`` suppresses user and request *context*, not the
locals attached to every frame of the traceback, and the SDK default is to
send them — so the promise and the configuration disagreed until #518.

``bot.py`` can't be imported from a unit test (module-level ``load_dotenv``,
a live ``commands.Bot``, a scheduler), so :class:`TestInitOptions` reads the
init call out of the source with ``ast``. :class:`TestNoLocalsLeaveTheProcess`
then proves the flag still means what that assertion assumes it means — one
test for the value we pass, one for what the SDK does with it.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest
import sentry_sdk
from sentry_sdk.utils import event_from_exception

from tests.constants import TEST_GUILD_ID

BOT_PY = Path(__file__).resolve().parents[2] / "bot.py"

# Not a real project. The transport below never sends anything anyway; the DSN
# only has to parse.
FAKE_DSN = "https://public@example.ingest.sentry.io/1"


def _sentry_init_kwargs() -> dict[str, ast.expr]:
    """The keywords ``bot.py`` passes to ``sentry_sdk.init``, unevaluated."""
    tree = ast.parse(BOT_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "init"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "sentry_sdk"
        ):
            return {kw.arg: kw.value for kw in node.keywords if kw.arg}
    pytest.fail(f"no sentry_sdk.init(...) call found in {BOT_PY.name}")


class TestInitOptions:
    def test_frame_locals_are_disabled(self):
        kwargs = _sentry_init_kwargs()
        assert "include_local_variables" in kwargs, (
            "sentry_sdk.init must pass include_local_variables — the SDK default "
            "is True, which ships every frame's locals and contradicts "
            "privacy.html's promise about crash reports (#518)"
        )
        assert ast.literal_eval(kwargs["include_local_variables"]) is False


class _CapturingTransport(sentry_sdk.transport.Transport):
    """Keeps envelopes in memory instead of shipping them to Sentry."""

    def __init__(self):
        super().__init__()
        self.events: list[dict] = []

    def capture_envelope(self, envelope):
        for item in envelope.items:
            if item.headers.get("type") == "event":
                self.events.append(json.loads(bytes(item.payload.get_bytes())))


def _handler(guild_id, user_id, member_name, credentials_json):
    """Shaped like the frames real events land in — see #453 / #455, both of
    which end inside ``config.get_spreadsheet`` with the service-account JSON
    in scope."""
    raise RuntimeError("boom")


def _frames_of_one_captured_event(**init_kwargs) -> list[dict]:
    """Raise inside ``_handler`` under ``init_kwargs`` and return the frames
    of the event that would have left the process."""
    transport = _CapturingTransport()
    client = sentry_sdk.Client(
        dsn=FAKE_DSN,
        traces_sample_rate=0.0,
        send_default_pii=False,
        transport=transport,
        **init_kwargs,
    )
    try:
        _handler(
            guild_id=TEST_GUILD_ID,
            user_id=999000111222333444,
            member_name="Dana",
            credentials_json='{"type": "service_account", "private_key": "not-a-real-key"}',
        )
    except RuntimeError:
        event, hint = event_from_exception(sys.exc_info(), client_options=client.options)
        client.capture_event(event, hint=hint)
    client.close()

    assert len(transport.events) == 1, "expected exactly one captured event"
    return transport.events[0]["exception"]["values"][-1]["stacktrace"]["frames"]


class TestNoLocalsLeaveTheProcess:
    def test_no_frame_carries_locals(self):
        frames = _frames_of_one_captured_event(include_local_variables=False)
        assert frames, "the traceback itself must still be reported"
        assert all(frame.get("vars") is None for frame in frames), (
            "a frame still carries locals: "
            f"{[f['function'] for f in frames if f.get('vars') is not None]}"
        )

    def test_the_sdk_default_would_have_carried_them(self):
        """The reason the flag is set. If this ever fails the SDK default has
        changed and the guard above is protecting against nothing — which is
        worth knowing, not worth silently keeping."""
        frames = _frames_of_one_captured_event()
        handler = next(f for f in frames if f["function"] == "_handler")
        assert "guild_id" in handler["vars"]
        assert "credentials_json" in handler["vars"]
