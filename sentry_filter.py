"""Keep Sentry's inbox to things that are actually our bugs.

Sentry is wired to open a GitHub issue for every high-priority event, so
anything that fires without being fixable turns into backlog noise and buries
real regressions. Six auto-filed issues were exactly that:

* #377 ``LoginFailure`` and #378 ``ConnectionClosed`` 4004, both raised out of
  ``bot.run()`` when Discord rejects the token. Same event reported twice, and
  a deploy-time misconfiguration rather than a code path anyone can fix.
* #416 ``DiscordServerError`` 503 while sending a survey question. Discord's
  side, and discord.py does not retry a 503 the way it retries 500/502/504.
* #382 / #383 the stats publisher's GitHub 503, handled in
  :mod:`stats_publisher` by retrying instead of filtering.

``before_send`` drops the first three and logs them instead, so the Railway
log still shows what happened. A rejected token is logged at CRITICAL because
the bot is down when it happens: dropping the Sentry event is a statement that
it is not a *bug*, not that it is unimportant.

Deliberately narrow. Only the exact upstream shapes above are dropped, so a
401 raised anywhere other than login, or an ``HTTPException`` we caused
ourselves, still pages normally.
"""

from __future__ import annotations

import logging

import discord

logger = logging.getLogger(__name__)

# Gateway close code Discord sends when it rejects the token. discord.py
# surfaces it as ConnectionClosed rather than LoginFailure when the REST
# login succeeded but the gateway handshake was refused.
AUTH_FAILED_CLOSE_CODE = 4004


def _exception_chain(exc: BaseException | None):
    """Yield ``exc`` and everything it was raised from, once each.

    ``LoginFailure`` arrives wrapping the 401 ``HTTPException`` that caused it,
    and which of the two the SDK hands us has changed across discord.py
    versions, so match against the whole chain. The ``seen`` guard is for
    self-referential ``__context__`` cycles, which are rare but would spin
    forever inside a ``before_send`` hook.
    """
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def drop_reason(exc: BaseException | None) -> str | None:
    """Why this exception should not reach Sentry, or ``None`` to send it.

    Returning a string rather than a bool so the caller can log *which* rule
    matched. A silent filter is its own debugging problem later.
    """
    for item in _exception_chain(exc):
        if isinstance(item, discord.LoginFailure):
            return "login-rejected"
        if (
            isinstance(item, discord.ConnectionClosed)
            and getattr(item, "code", None) == AUTH_FAILED_CLOSE_CODE
        ):
            return "gateway-auth-rejected"
        if isinstance(item, discord.DiscordServerError):
            return "discord-5xx"
    return None


# Rules whose cause is a dead bot rather than a passing blip. Logged loudly,
# because nothing else will say it once the Sentry event is gone.
_FATAL_REASONS = frozenset({"login-rejected", "gateway-auth-rejected"})

_REASON_DETAIL = {
    "login-rejected": (
        "Discord rejected DISCORD_TOKEN. The bot cannot start. Check the token "
        "in the Railway service's environment variables."
    ),
    "gateway-auth-rejected": (
        "Discord closed the gateway with 4004 (authentication failed). Check "
        "DISCORD_TOKEN in the Railway service's environment variables, and that "
        "the privileged intents this bot needs are still enabled in the "
        "Developer Portal."
    ),
    "discord-5xx": "Discord returned a server error. Transient, nothing to fix on our side.",
}


def before_send(event, hint):
    """Sentry ``before_send`` hook. Return the event to send it, ``None`` to drop.

    Never raises: a hook that throws costs us the event *and* the diagnosis, so
    an unexpected shape falls through to sending, which is the safe direction.
    """
    try:
        exc_info = (hint or {}).get("exc_info")
        exc = exc_info[1] if exc_info else None
        reason = drop_reason(exc)
        if reason is None:
            return event
        detail = _REASON_DETAIL.get(reason, "")
        if reason in _FATAL_REASONS:
            logger.critical("[SENTRY] dropped %s: %s (%s)", reason, detail, exc)
        else:
            logger.warning("[SENTRY] dropped %s: %s (%s)", reason, detail, exc)
        return None
    except Exception:  # noqa: BLE001 - see docstring; never break error reporting
        return event
