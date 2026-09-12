"""
wizard_registry.py — shared cancel-event registry, and the shared base views

Any long-running interactive flow (setup wizard, train schedule wizard,
storm participation log, etc.) registers a per-user asyncio.Event when it
starts and unregisters when it ends. The /cancel command sets every
registered event for the user, allowing each flow to bail out cleanly.

It also owns the two view base classes every hub, picker and confirm
inherits from: `ExpiringView` (cleans up on timeout, adds buttons) and
`OwnedView` (the same, usable only by the person who opened it). They
replaced seventy-odd pasted copies of the same two methods on 2026-09-11
(#589); do not write a new copy of either.

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
import time

import discord

from messages import (
    BTN_PAGE_LABEL,
    BTN_PAGE_NEXT,
    BTN_PAGE_PREV,
    DENY_NOT_OWNER,
    VIEW_TIMEOUT,
    VIEW_TIMEOUT_NO_HINT,
)

#: How long after an ephemeral message is sent it can still be edited. The
#: interaction token lives 15 minutes; a minute is kept back for the timeout
#: task to wake and the edit to land.
TOKEN_WINDOW = 14 * 60

# user_id -> list of asyncio.Event objects (one per active flow)
_active: dict[int, list[asyncio.Event]] = {}


async def safe_edit_response(interaction: discord.Interaction, **kwargs) -> None:
    """Edit the originating component message, surviving interaction-token expiry.

    Discord interaction tokens are valid for 3 seconds. If a wizard view's
    button or select callback can't reach `interaction.response.edit_message`
    in that window (slow network, idle wizard, busy event loop), Discord
    rejects the response with `NotFound` (10062 "Unknown interaction"). Without
    a fallback the exception propagates, `self.stop()` never runs, and the
    wizard hangs on `view.wait()` until the view timeout fires.

    This helper tries the normal interaction response first, then falls back to
    `interaction.message.edit` so the disabled/updated view still renders and
    the caller's `self.stop()` runs unconditionally. Accepts the same keyword
    arguments as either underlying call (content, view, embed, embeds, …).
    """
    try:
        await interaction.response.edit_message(**kwargs)
    except discord.NotFound:
        try:
            await interaction.message.edit(**kwargs)
        except discord.HTTPException:
            pass


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
    main_task = (
        asyncio.create_task(awaitable) if not isinstance(awaitable, asyncio.Task) else awaitable
    )
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

    `command_hint` is the pre-formatted re-initiate hint shown to
    leadership. Callers own the formatting — wrap the slash command in
    backticks yourself (e.g. "`/events`") or include button-label
    markdown (e.g. "`/desertstorm` → **👀 View sign-ups + set up teams**").
    Leave empty to drop the suffix.

    Idempotent — re-running on the same message is a no-op. Swallows
    discord errors (deleted message, lost perms) so the timeout
    handler never raises.
    """
    if message is None:
        return
    line = VIEW_TIMEOUT.format(hint=command_hint) if command_hint else VIEW_TIMEOUT_NO_HINT
    notice = f"\n\n*{line}*"
    try:
        existing = getattr(message, "content", None) or ""
        if "actions for this have timed out" in existing:
            return
        await message.edit(content=existing + notice, view=None)
    except Exception:
        pass


class PaginationRow:
    """The three buttons `ExpiringView.add_pagination_row` added.

    A view that rebuilds its items on every page turn can ignore this. One
    that updates controls in place instead (the transfer setup pickers
    re-point a select rather than rebuilding) calls `sync` after changing
    the page, so the arrows and the count follow.
    """

    def __init__(
        self,
        prev: discord.ui.Button,
        label: discord.ui.Button,
        next_: discord.ui.Button,
        page_count: int,
    ):
        self.prev = prev
        self.label = label
        self.next = next_
        self.page_count = page_count

    def sync(self, page: int) -> None:
        self.prev.disabled = page <= 0
        self.next.disabled = page >= self.page_count - 1
        self.label.label = BTN_PAGE_LABEL.format(n=page + 1, m=self.page_count)


class ExpiringView(discord.ui.View):
    """A view that cleans up after itself when it times out.

    Capture the message after sending (``view.message = await ch.send(...)``
    or ``await inter.original_response()``) and set ``timeout_hint`` to the
    route back, pre-formatted the way `expire_view_message` documents (for a
    hub button, `messages.ROUTE_HINT`). When the timeout fires the buttons
    come off and the notice goes on, so nobody is left clicking a dead
    control. Every view that can time out declares a hint (settled
    2026-09-12, #589); the two that keep their own handler are named in
    `CLAUDE.md`. A view with no hint and no handler does nothing on timeout.

    ``timeout_hint`` can be a class attribute, passed to ``__init__``, or
    overridden as a property when the hint depends on the event type or
    lives on a parent view. ``add_button`` and ``add_pagination_row`` are
    the shorthands every hub used to write for itself.

    An ephemeral message can be edited only for `TOKEN_WINDOW` after it was
    sent, and Discord restarts a view's timer on every click, so a 10-minute
    picker worked for 20 minutes would time out after the notice could land.
    The base keeps that from happening: once it holds an ephemeral message
    it shrinks its own timer on each click so the timeout always fires
    inside the window. The officer keeps the full timeout per click until
    the window nears its end.
    """

    timeout_hint: str | None = None
    _message: discord.Message | None = None
    _sent_at: float | None = None

    def __init__(self, *, timeout: float | None = 180.0, timeout_hint: str | None = None):
        super().__init__(timeout=timeout)
        if timeout_hint is not None:
            self.timeout_hint = timeout_hint

    @property
    def message(self) -> discord.Message | None:
        return self._message

    @message.setter
    def message(self, value: discord.Message | None) -> None:
        self._message = value
        self._sent_at = time.monotonic() if value is not None else None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        self._keep_inside_token_window()
        return True

    def _keep_inside_token_window(self) -> None:
        msg = self._message
        if msg is None or self._sent_at is None or not self.timeout:
            return
        if getattr(getattr(msg, "flags", None), "ephemeral", None) is not True:
            return
        left = TOKEN_WINDOW - (time.monotonic() - self._sent_at)
        if left < self.timeout:
            self.timeout = max(1.0, left)

    async def on_timeout(self) -> None:
        if self.timeout_hint is None:
            return
        await expire_view_message(self.message, command_hint=self.timeout_hint)

    def add_button(
        self,
        label: str,
        style: discord.ButtonStyle,
        callback,
        *,
        row: int | None = None,
        disabled: bool = False,
    ) -> discord.ui.Button:
        """Add a callback-bound button. Labels are clamped to Discord's 80."""
        button = discord.ui.Button(label=label[:80], style=style, row=row, disabled=disabled)
        button.callback = callback
        self.add_item(button)
        return button

    def add_pagination_row(
        self,
        *,
        page: int,
        page_count: int,
        on_page,
        row: int | None = None,
        next_label: str = BTN_PAGE_NEXT,
    ) -> PaginationRow | None:
        """Prev, "Page n / m", Next, when there is more than one page.

        `on_page(interaction, page)` is the view's own async page turn: store
        the page, rebuild or re-point the controls, and edit the message. It
        only ever receives a page inside range, because the arrow at either
        end is disabled. Below two pages nothing is added and `None` comes
        back, which is every earlier copy's behaviour. `next_label` exists for
        the team-plan pickers, where "Next" would read as the next step.
        """
        if page_count <= 1:
            return None

        async def _prev(inter: discord.Interaction) -> None:
            await on_page(inter, max(0, page - 1))

        async def _next(inter: discord.Interaction) -> None:
            await on_page(inter, min(page_count - 1, page + 1))

        prev = self.add_button(
            BTN_PAGE_PREV, discord.ButtonStyle.secondary, _prev, row=row, disabled=page <= 0
        )
        label = discord.ui.Button(
            label=BTN_PAGE_LABEL.format(n=page + 1, m=page_count),
            style=discord.ButtonStyle.secondary,
            row=row,
            disabled=True,
        )
        self.add_item(label)
        next_ = self.add_button(
            next_label,
            discord.ButtonStyle.secondary,
            _next,
            row=row,
            disabled=page >= page_count - 1,
        )
        return PaginationRow(prev, label, next_, page_count)


class OwnedView(ExpiringView):
    """An `ExpiringView` only the person who opened it may use.

    Set ``owner_id`` in ``__init__``, or override it as a property where the
    owner lives on a parent view or a builder session. Anyone else who
    presses a control is told `messages.DENY_NOT_OWNER`, ephemerally, and
    the callback never runs. A view that never sets an owner refuses
    everyone rather than admitting everyone, so a forgotten owner shows up
    on the first click in testing instead of in production.
    """

    owner_id: int | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(DENY_NOT_OWNER, ephemeral=True)
            return False
        return await super().interaction_check(interaction)


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
    view_task = asyncio.create_task(view.wait())
    try:
        done, pending = await asyncio.wait(
            [view_task, cancel_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_task in done:
            view.cancelled = True
            try:
                view.stop()  # makes the view's own wait() return
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
