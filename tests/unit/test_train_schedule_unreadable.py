"""A failed read of the Train Schedule tab never leads to a save (#716).

`save_schedule` rewrites the whole tab from the dict it is given. When
`load_schedule` swallowed a read error into `{}`, the next save emptied the
tab: one passing rate limit before the nightly birthday auto-add was enough.
Every path that saves now loads with `strict=True`, which raises
`ScheduleUnreadable` instead, and each one stops without saving.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

GUILD_ID = 12345


def _interaction():
    interaction = MagicMock()
    interaction.user.id = 9001
    interaction.guild_id = GUILD_ID
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _unreadable(*_a, **kw):
    from train import ScheduleUnreadable

    if kw.get("strict"):
        raise ScheduleUnreadable("429")
    return {}


class TestLoadSchedule:
    def test_a_failed_read_is_empty_for_display_and_raises_for_a_save(self):
        from train import ScheduleUnreadable, load_schedule

        with (
            patch("train._get_train_sheet", MagicMock(side_effect=RuntimeError("boom"))),
            patch("train._note_train_sheet_error"),
            patch("train._train_tab_name", return_value="Train Schedule"),
        ):
            assert load_schedule(GUILD_ID) == {}
            with pytest.raises(ScheduleUnreadable):
                load_schedule(GUILD_ID, strict=True)

    def test_a_clean_read_is_the_same_either_way(self):
        from train import load_schedule

        ws = MagicMock()
        ws.get_all_values.return_value = [
            ["Date", "Name", "Theme", "Tone", "Notes", "Prompt Retrieved"],
            ["2026-10-01", "Alpha", "", "", "", "FALSE"],
        ]
        with (
            patch("train._get_train_sheet", MagicMock(return_value=ws)),
            patch("train._note_train_sheet_ok"),
        ):
            assert load_schedule(GUILD_ID, strict=True) == load_schedule(GUILD_ID)


@pytest.mark.parametrize("modal_name", ["AddEntryModal", "UpdateEntryModal"])
async def test_an_entry_modal_saves_nothing_over_an_unread_tab(modal_name):
    import train_ui
    from messages import TRAIN_SCHEDULE_UNREADABLE

    interaction = _interaction()
    if modal_name == "AddEntryModal":
        modal = train_ui.AddEntryModal(MagicMock(), GUILD_ID, blurbs_enabled=False)
    else:
        modal = train_ui.UpdateEntryModal(
            MagicMock(), GUILD_ID, False, "2026-10-01", {"name": "Alpha"}
        )
    modal.date_input._value = "10/2"
    modal.name_input._value = "Bravo"
    save = MagicMock()
    with (
        patch("train_ui.load_schedule", side_effect=_unreadable),
        patch("train_ui.save_schedule", save),
        patch("train_ui._train_tab_name", return_value="Train Schedule"),
    ):
        await modal.on_submit(interaction)
    save.assert_not_called()
    interaction.followup.send.assert_awaited_once_with(
        TRAIN_SCHEDULE_UNREADABLE.format(tab="Train Schedule"), ephemeral=True
    )


async def test_the_birthday_check_button_saves_nothing_over_an_unread_tab():
    import train_hub
    from messages import TRAIN_SCHEDULE_UNREADABLE

    interaction = _interaction()
    save = MagicMock()
    with (
        patch("train.load_schedule", side_effect=_unreadable),
        patch("train.save_schedule", save),
        patch("train._train_tab_name", return_value="Train Schedule"),
    ):
        await train_hub._run_birthday_check(MagicMock(), interaction)
    save.assert_not_called()
    interaction.followup.send.assert_awaited_once_with(
        TRAIN_SCHEDULE_UNREADABLE.format(tab="Train Schedule"), ephemeral=True
    )
