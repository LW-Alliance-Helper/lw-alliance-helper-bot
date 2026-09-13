"""
db_timings.py — how long each config-database call takes, per calling helper

The measurement behind the step 10 decision on
https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/589:
284 coroutine call sites read the config database through `config.*`
helpers without a thread hand-off. Whether that is a convention to bless
or a class of bug to fix depends on one number nobody had: how long a
config read actually blocks the event loop on Railway's volume.

`config._get_conn` opens every connection through `TimedConnection`, which
records the time from the open to the end of the ``with`` block (or
``close()``), keyed by the helper that opened it, and notes whether the
call ran on the event loop's thread or off it. Nothing is written per call:
the numbers live in this process and `/admin db_timings` reads them. A call
over `SLOW_MS` logs one warning per helper per ten minutes, so a quietly
slow helper shows up in Railway's logs without flooding them.

Cost of the measurement itself: one `perf_counter` pair, one frame lookup
and one dict update per call, under a lock. Microseconds against the
milliseconds being measured.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import sqlite3
import sys
import threading
import time

logger = logging.getLogger(__name__)

#: A call this long or longer is logged (once per helper per `WARN_EVERY_S`)
#: and kept in the recent-slow list. 50 ms is far above any healthy SQLite
#: read and well below what a person would notice as a hitch.
SLOW_MS = 50.0
WARN_EVERY_S = 600.0

#: Upper bounds of the histogram buckets, in ms; the last bucket is open.
BUCKETS = (1.0, 5.0, 20.0, 100.0)
BUCKET_LABELS = ("<1", "<5", "<20", "<100", "≥100")


class Stat:
    __slots__ = ("calls", "on_loop", "total_ms", "loop_ms", "max_ms", "buckets")

    def __init__(self) -> None:
        self.calls = 0
        self.on_loop = 0
        self.total_ms = 0.0
        self.loop_ms = 0.0
        self.max_ms = 0.0
        self.buckets = [0] * (len(BUCKETS) + 1)

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.calls if self.calls else 0.0


_lock = threading.Lock()
_stats: dict[str, Stat] = {}
_recent_slow: collections.deque = collections.deque(maxlen=20)
_last_warned: dict[str, float] = {}
_since = time.time()


def on_event_loop() -> bool:
    """True when this thread is running an asyncio loop, i.e. a blocking call
    here is blocking the bot. False in a `to_thread` worker."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def caller_name(depth: int = 2) -> str:
    """The name of the function `depth` frames up: with the default, the
    function that called the function that called this."""
    try:
        return sys._getframe(depth).f_code.co_name
    except ValueError:
        return "?"


def record(helper: str, ms: float, on_loop: bool) -> None:
    with _lock:
        stat = _stats.get(helper)
        if stat is None:
            stat = _stats[helper] = Stat()
        stat.calls += 1
        stat.total_ms += ms
        if on_loop:
            stat.on_loop += 1
            stat.loop_ms += ms
        if ms > stat.max_ms:
            stat.max_ms = ms
        for i, bound in enumerate(BUCKETS):
            if ms < bound:
                stat.buckets[i] += 1
                break
        else:
            stat.buckets[-1] += 1
        if ms >= SLOW_MS:
            now = time.time()
            _recent_slow.append((now, helper, ms, on_loop))
            warn = now - _last_warned.get(helper, 0.0) >= WARN_EVERY_S
            if warn:
                _last_warned[helper] = now
    if ms >= SLOW_MS and warn:
        logger.warning(
            "[DB TIMINGS] %s took %.0f ms %s",
            helper,
            ms,
            "on the event loop" if on_loop else "off the loop",
        )


def snapshot() -> dict:
    """A copy for rendering: helpers sorted by time spent on the loop."""
    with _lock:
        rows = sorted(
            ((name, s) for name, s in _stats.items()),
            key=lambda ns: (ns[1].loop_ms, ns[1].total_ms),
            reverse=True,
        )
        return {
            "since": _since,
            "helpers": [
                {
                    "helper": name,
                    "calls": s.calls,
                    "on_loop": s.on_loop,
                    "avg_ms": s.avg_ms,
                    "max_ms": s.max_ms,
                    "loop_ms": s.loop_ms,
                    "buckets": list(s.buckets),
                }
                for name, s in rows
            ],
            "recent_slow": list(_recent_slow),
        }


def reset() -> None:
    global _since
    with _lock:
        _stats.clear()
        _recent_slow.clear()
        _last_warned.clear()
        _since = time.time()


class TimedConnection(sqlite3.Connection):
    """A connection that reports how long it was held. `config._get_conn`
    opens with ``factory=TimedConnection`` and calls `start`; the ``with``
    block's exit (or an explicit ``close``) records the elapsed time once."""

    _helper = "?"
    _t0 = 0.0
    _on_loop = False
    _recorded = False

    def start(self, t0: float, helper: str, on_loop: bool) -> None:
        self._t0 = t0
        self._helper = helper
        self._on_loop = on_loop

    def _finish(self) -> None:
        if self._recorded:
            return
        self._recorded = True
        record(self._helper, (time.perf_counter() - self._t0) * 1000.0, self._on_loop)

    def __exit__(self, *exc):
        try:
            return super().__exit__(*exc)
        finally:
            self._finish()

    def close(self) -> None:
        self._finish()
        super().close()
