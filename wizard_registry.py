"""
wizard_registry.py — shared cancel-event registry for active sessions

Any long-running interactive flow (setup wizard, train schedule wizard,
storm participation log, etc.) registers a per-user asyncio.Event when it
starts and unregisters when it ends. The /cancel command sets every
registered event for the user, allowing each flow to bail out cleanly.

Usage in a wizard:

    cancel_event = wizard_registry.register(user.id)
    try:
        # race wait_for against cancel_event in each step:
        if await wizard_registry.wait_or_cancel(
            bot.wait_for("message", check=check, timeout=120),
            cancel_event,
        ) is None:
            return  # cancelled or timed out
        ...
    finally:
        wizard_registry.unregister(user.id, cancel_event)
"""

import asyncio

# user_id -> list of asyncio.Event objects (one per active flow)
_active: dict[int, list[asyncio.Event]] = {}


def register(user_id: int) -> asyncio.Event:
    """Register a new cancellable session for this user. Returns the event."""
    ev = asyncio.Event()
    _active.setdefault(user_id, []).append(ev)
    return ev


def unregister(user_id: int, event: asyncio.Event) -> None:
    """Remove a session's cancel event. Safe to call even if already removed."""
    bucket = _active.get(user_id)
    if not bucket:
        return
    try:
        bucket.remove(event)
    except ValueError:
        pass
    if not bucket:
        _active.pop(user_id, None)


def cancel_user(user_id: int) -> bool:
    """Set every registered cancel event for this user. Returns True if any were active."""
    bucket = _active.pop(user_id, [])
    for ev in bucket:
        ev.set()
    return bool(bucket)


def is_active(user_id: int) -> bool:
    return user_id in _active


async def wait_or_cancel(awaitable, cancel_event: asyncio.Event):
    """
    Race an awaitable against a cancel event.
    Returns the awaitable's result, or None if the cancel event fires first
    (or the awaitable raises asyncio.TimeoutError).
    """
    main_task   = asyncio.create_task(awaitable) if not isinstance(awaitable, asyncio.Task) else awaitable
    cancel_task = asyncio.create_task(cancel_event.wait())
    try:
        done, pending = await asyncio.wait(
            [main_task, cancel_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        if cancel_task in done:
            return None
        try:
            return main_task.result()
        except asyncio.TimeoutError:
            return None
    except asyncio.CancelledError:
        main_task.cancel()
        cancel_task.cancel()
        raise
