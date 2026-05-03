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


async def expire_view_message(message, command_hint: str = "") -> None:
    """
    Strip buttons from a previously-posted message and append a friendly
    timeout notice. Called from a discord.ui.View's `on_timeout` after
    the view captured its own message (`view.message = await ch.send(...)`).

    Without this, expired views still render apparently-active buttons —
    clicking one fails with "Interaction failed" because the view
    stopped listening on the bot side. The user just sees a dead UI.

    `command_hint` is the slash command leadership should run to
    re-initiate the flow (e.g. "/events", "/train"). Leave empty to
    drop the suffix.

    Idempotent — re-running on the same message is a no-op. Swallows
    discord errors (deleted message, lost perms) so the timeout
    handler never raises.
    """
    if message is None:
        return
    suffix = f" Use `{command_hint}` to re-initiate." if command_hint else ""
    notice = f"\n\n⏰ *The actions for this have timed out.{suffix}*"
    try:
        existing = (getattr(message, "content", None) or "")
        if "actions for this have timed out" in existing:
            return
        await message.edit(content=existing + notice, view=None)
    except Exception:
        pass


async def wait_view_or_cancel(view, cancel_event):
    """Race a discord.ui.View's wait() against a cancel event.

    The bare `view.wait()` only returns when the view's internal timeout
    fires or the view is stopped — it doesn't know about the wizard
    registry. So when /cancel is invoked mid-step, the wizard's view
    sits there until ITS own timeout fires, at which point the wizard
    posts a "Timed out" message even though the user actually cancelled.

    This helper:
      * If `cancel_event` is None → behaves exactly like view.wait()
        (used as a defensive default; some callsites may not have a
        cancel_event in scope).
      * Otherwise races view.wait() against cancel_event. If the cancel
        event fires first, calls view.stop() (which makes view.wait()
        return) and sets `view.cancelled = True` so the wizard can
        distinguish a /cancel from a true timeout.

    The wizard pattern after the call:

        await wait_view_or_cancel(view, cancel_event)
        if view.cancelled:
            return                              # silent bail, no timeout msg
        if not view.confirmed:
            await channel.send("⏰ Timed out…")
            return
        # ... use view.selected_X
    """
    # Initialise the attribute so callers can rely on its presence.
    view.cancelled = False

    if cancel_event is None:
        await view.wait()
        return

    cancel_task = asyncio.create_task(cancel_event.wait())
    view_task   = asyncio.create_task(view.wait())
    try:
        done, pending = await asyncio.wait(
            [view_task, cancel_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_task in done:
            view.cancelled = True
            try:
                view.stop()                     # makes the view's own wait() return
            except Exception:
                pass
            for t in pending:
                t.cancel()
        else:
            cancel_task.cancel()
    except asyncio.CancelledError:
        view_task.cancel()
        cancel_task.cancel()
        raise
