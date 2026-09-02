"""
Per-event 5-minute warning text (#566).

`scheduler.build_warning_message` read a `warning_blurb` field from the
start, but nothing wrote it: no column, no wizard step, no export field.
The branch was dead, so every alliance got the generic line. These tests
cover the field end to end, and pin the decision that its placeholders
resolve to the event's real clock time rather than to "5 minutes" — which
is the mistake that produced #565.
"""

from datetime import datetime, timedelta, timezone

import pytest

TEST_GUILD_ID = 987654321

# Fixed instant, never "now". A wall-clock read here would make the
# rendered {time}/{server_time} assertions drift with the run date.
ET = timezone(timedelta(hours=-4))
EVENT_DT = datetime(2026, 5, 15, 21, 0, tzinfo=ET)


def _event(key="test_event", name="Test Event", warning_blurb="", blurb=None):
    return [
        {
            "key": key,
            "name": name,
            "dt": EVENT_DT,
            "blurb": blurb if blurb is not None else f"{name} at {{time}} ({{server_time}}).",
            "warning_blurb": warning_blurb,
        }
    ]


def _base_event_row(**overrides):
    row = {
        "short_key": "ae_plague_marauder",
        "name": "Alliance Exercise: Plague Marauder",
        "timezone": "America/New_York",
        "default_time": "22:00",
        "announcement_blurb": "{name} at {time} ({server_time} Server Time).",
        "warning_blurb": "",
        "schedule_type": "repeating",
        "anchor_date": "2026-05-01",
        "interval_days": 3,
        "draft_channel_id": 0,
        "announcement_channel_id": 0,
        "draft_time": "12:00",
        "five_min_warning": 1,
        "active": 1,
    }
    row.update(overrides)
    return row


class TestWarningBlurbRendering:
    """What the warning actually posts."""

    def test_custom_warning_renders_the_real_event_time(self):
        """The #566 decision, and the guard against repeating #565.

        A warning blurb's {time} is the event's clock time, exactly as it
        is in an announcement. It is NOT the string "5 minutes" — that
        substitution is what produced "Plague Marauder at 5 minutes
        (5 minutes Server Time)."
        """
        from scheduler import build_warning_message, format_et, to_server_time_str

        msg = build_warning_message(
            _event(warning_blurb="Marauder starts at {time} ({server_time} Server Time). Get on."),
        )

        assert format_et(EVENT_DT) in msg
        assert to_server_time_str(EVENT_DT) in msg
        assert "at 5 minutes" not in msg
        assert "5 minutes Server Time" not in msg
        assert "{time}" not in msg

    def test_custom_warning_supports_the_name_placeholder(self):
        from scheduler import build_warning_message

        msg = build_warning_message(
            _event(name="Zombie Siege", warning_blurb="{name} in five. Walls up."),
        )
        assert msg == "Zombie Siege in five. Walls up."

    def test_empty_warning_blurb_falls_to_the_default(self):
        from scheduler import WARNING_BLURB_DEFAULT, build_warning_message

        msg = build_warning_message(_event(name="Zombie Siege", warning_blurb=""))
        assert msg == WARNING_BLURB_DEFAULT.format(name="Zombie Siege")

    def test_unknown_placeholder_falls_back_instead_of_raising(self):
        """A placeholder we do not support is not a reason to post nothing.
        `{start_time}` is the plausible guess an officer makes when the
        real name is `{time}`. `build_warning_message` runs inside the
        scheduler loop, so an uncaught KeyError takes the warning down."""
        from scheduler import WARNING_BLURB_DEFAULT, build_warning_message

        msg = build_warning_message(
            _event(name="Zombie Siege", warning_blurb="Starts at {start_time}, be online."),
        )
        assert msg == WARNING_BLURB_DEFAULT.format(name="Zombie Siege")

    def test_custom_warning_beats_the_legacy_marauder_string(self):
        from scheduler import build_warning_message

        msg = build_warning_message(
            _event(key="marauder", name="Marauder (AE)", warning_blurb="Ours, not the hardcoded."),
        )
        assert msg == "Ours, not the hardcoded."

    def test_legacy_marauder_string_survives_an_empty_warning_blurb(self):
        from scheduler import build_warning_message

        msg = build_warning_message(_event(key="marauder", name="Marauder (AE)"))
        assert "hop online and get your points" in msg

    def test_announcement_blurb_is_never_reused(self):
        """#565. The announcement blurb must not reach the warning at all,
        however tempting its wording looks."""
        from scheduler import WARNING_BLURB_DEFAULT, build_warning_message

        msg = build_warning_message(
            _event(
                name="Glacieradon",
                blurb="Glacieradon at {time}. Get in voice five minutes before the start.",
            )
        )
        assert msg == WARNING_BLURB_DEFAULT.format(name="Glacieradon")
        assert "voice" not in msg


class TestWarningBlurbPersistence:
    """The column, and the round trip through config."""

    def test_column_round_trips(self, temp_db):
        import config

        config.save_guild_event(
            TEST_GUILD_ID, _base_event_row(warning_blurb="{name} in 5. Offline participation on.")
        )
        loaded = config.get_guild_event(TEST_GUILD_ID, "ae_plague_marauder")
        assert loaded["warning_blurb"] == "{name} in 5. Offline participation on."

    def test_defaults_to_empty_string_not_null(self, temp_db):
        """'' means "has not chosen", which is what lets the wizard label
        the generic line as the default rather than showing it back as a
        saved value. A NULL here would break `.strip()` downstream."""
        import config

        row = _base_event_row()
        del row["warning_blurb"]
        config.save_guild_event(TEST_GUILD_ID, row)
        loaded = config.get_guild_event(TEST_GUILD_ID, "ae_plague_marauder")
        assert loaded["warning_blurb"] == ""

    def test_export_carries_the_field(self, temp_db):
        """An alliance that moves servers keeps its warning wording. The
        blurb travels; channel and role ids are remapped separately."""
        import config
        import config_export

        config.save_guild_event(
            TEST_GUILD_ID, _base_event_row(warning_blurb="Siege in 5. Walls up.")
        )
        events = config_export.collect_events(
            TEST_GUILD_ID,
            channel_lookup=lambda cid: f"#chan-{cid}",
            role_lookup=lambda rid: f"@role-{rid}",
        )
        assert events, "export produced no events"
        # Each entry is {"travels": {...}, "remap_channels": [...], ...};
        # the blurb is content, so it rides in `travels` untouched.
        assert events[0]["travels"]["warning_blurb"] == "Siege in 5. Walls up."

    def test_import_defaults_the_field_when_absent(self, temp_db):
        """An export written before this field existed must still import.
        `.get(..., "")` is what makes the older file loadable rather than
        a KeyError halfway through someone's migration."""
        import inspect

        import config_export

        src = inspect.getsource(config_export)
        assert '"warning_blurb": t.get("warning_blurb", "")' in src


class TestWarningDefaultIsSharedNotRetyped:
    """The wizard's preview and the posted warning have to be the same
    string. They live in one constant so they cannot drift."""

    def test_events_hub_imports_the_scheduler_constant(self):
        import inspect

        import events_hub

        src = inspect.getsource(events_hub)
        assert "from scheduler import WARNING_BLURB_DEFAULT" in src
        # The literal must not be retyped anywhere in the wizard.
        assert "in 5 minutes! Make sure you're online." not in src

    def test_default_names_the_event(self):
        from scheduler import WARNING_BLURB_DEFAULT

        rendered = WARNING_BLURB_DEFAULT.format(name="Zombie Siege")
        assert rendered.startswith("Zombie Siege")
        assert "{name}" not in rendered


class TestWarningBlurbMigration:
    """The column has to arrive on databases that already exist.

    `temp_db` builds a fresh schema, which is the easy case: CREATE TABLE
    already names the column. Every real alliance is the other case, a
    guild_events table written before this shipped, and the ALTER TABLE in
    init_db is the only thing that reaches them.
    """

    def test_alter_table_adds_the_column_to_a_pre_existing_table(self, tmp_path, monkeypatch):
        import sqlite3

        import config

        db_path = str(tmp_path / "legacy.db")

        # A guild_events table exactly as it looked before #566: every
        # column except warning_blurb.
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE guild_events (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id                INTEGER NOT NULL,
                short_key               TEXT    NOT NULL,
                name                    TEXT    NOT NULL,
                timezone                TEXT    NOT NULL DEFAULT 'America/New_York',
                default_time            TEXT    NOT NULL DEFAULT '22:00',
                announcement_blurb      TEXT    NOT NULL DEFAULT '',
                schedule_type           TEXT    NOT NULL DEFAULT 'repeating',
                anchor_date             TEXT    DEFAULT '',
                interval_days           INTEGER DEFAULT 3,
                draft_channel_id        INTEGER DEFAULT 0,
                announcement_channel_id INTEGER DEFAULT 0,
                draft_time              TEXT    DEFAULT '12:00',
                five_min_warning        INTEGER DEFAULT 1,
                active                  INTEGER DEFAULT 1,
                UNIQUE(guild_id, short_key)
            )
            """
        )
        conn.execute(
            "INSERT INTO guild_events (guild_id, short_key, name, announcement_blurb) "
            "VALUES (?, ?, ?, ?)",
            (TEST_GUILD_ID, "ae_plague_marauder", "Alliance Exercise: Plague Marauder", "hi"),
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr(config, "DB_PATH", db_path)
        config.init_db()

        with sqlite3.connect(db_path) as check:
            check.row_factory = sqlite3.Row
            cols = {r["name"] for r in check.execute("PRAGMA table_info(guild_events)")}
            assert "warning_blurb" in cols

            row = check.execute(
                "SELECT warning_blurb, name FROM guild_events WHERE short_key = ?",
                ("ae_plague_marauder",),
            ).fetchone()
            # The existing row backfills to '' (not NULL), so the wizard
            # reads it as "has not chosen" and the scheduler's .strip()
            # does not blow up on a None.
            assert row["warning_blurb"] == ""
            assert row["name"] == "Alliance Exercise: Plague Marauder"

    def test_migration_is_idempotent(self, tmp_path, monkeypatch):
        """init_db runs on every boot. The second run must be a no-op, not
        a duplicate-column error that takes startup down."""
        import config

        db_path = str(tmp_path / "twice.db")
        monkeypatch.setattr(config, "DB_PATH", db_path)
        config.init_db()
        config.init_db()  # would raise if the ALTER were not guarded


class TestWarningBlurbEditFlow:
    """`✏️ Edit 5-minute warning` on the events hub.

    The create wizard asks the question once, and no alliance re-runs it
    for an event that already exists — which, when this shipped, was all
    of them. Without this surface the only way to word a warning was to
    delete the event and rebuild it, losing its anchor date and its
    announcement wording to change one line.
    """

    def test_setter_writes_and_clears(self, temp_db):
        import config

        config.save_guild_event(TEST_GUILD_ID, _base_event_row())

        assert config.set_guild_event_warning_blurb(
            TEST_GUILD_ID, "ae_plague_marauder", "Marauder in 5. Offline participation on."
        )
        loaded = config.get_guild_event(TEST_GUILD_ID, "ae_plague_marauder")
        assert loaded["warning_blurb"] == "Marauder in 5. Offline participation on."

        # Clearing is a real action, not a no-op: '' is the only route back
        # to the default once an alliance has written their own.
        assert config.set_guild_event_warning_blurb(TEST_GUILD_ID, "ae_plague_marauder", "")
        loaded = config.get_guild_event(TEST_GUILD_ID, "ae_plague_marauder")
        assert loaded["warning_blurb"] == ""

    def test_setter_reports_a_missing_event(self, temp_db):
        import config

        assert config.set_guild_event_warning_blurb(TEST_GUILD_ID, "no_such_event", "x") is False

    def test_clearing_returns_the_event_to_the_default(self, temp_db):
        """End to end: write, clear, and confirm the posted warning is the
        default again rather than an empty message."""
        import config
        from scheduler import WARNING_BLURB_DEFAULT, build_warning_message

        config.save_guild_event(TEST_GUILD_ID, _base_event_row(warning_blurb="Custom line."))
        config.set_guild_event_warning_blurb(TEST_GUILD_ID, "ae_plague_marauder", "")

        row = config.get_guild_event(TEST_GUILD_ID, "ae_plague_marauder")
        msg = build_warning_message(
            _event(
                key="ae_plague_marauder",
                name=row["name"],
                warning_blurb=row["warning_blurb"],
            )
        )
        assert msg == WARNING_BLURB_DEFAULT.format(name="Alliance Exercise: Plague Marauder")
        assert msg.strip()

    @pytest.mark.asyncio
    async def test_modal_submit_saves_and_confirms(self, temp_db):
        from unittest.mock import AsyncMock, MagicMock

        import config
        from events_hub import _WarningBlurbModal

        config.save_guild_event(TEST_GUILD_ID, _base_event_row())

        modal = _WarningBlurbModal(
            TEST_GUILD_ID, "ae_plague_marauder", "Alliance Exercise: Plague Marauder", ""
        )
        modal.field._value = "Marauder in 5. Offline participation on."

        inter = MagicMock()
        inter.response.edit_message = AsyncMock()
        await modal.on_submit(inter)

        assert (
            config.get_guild_event(TEST_GUILD_ID, "ae_plague_marauder")["warning_blurb"]
            == "Marauder in 5. Offline participation on."
        )
        body = inter.response.edit_message.await_args.kwargs["content"]
        assert "Marauder in 5. Offline participation on." in body

    @pytest.mark.asyncio
    async def test_modal_submit_empty_clears_and_says_so(self, temp_db):
        """An empty submit is a clear, and the confirmation has to show
        what they get instead. Saying only "cleared" would leave the
        officer guessing what now posts."""
        from unittest.mock import AsyncMock, MagicMock

        import config
        from events_hub import _WarningBlurbModal
        from scheduler import WARNING_BLURB_DEFAULT

        config.save_guild_event(TEST_GUILD_ID, _base_event_row(warning_blurb="Old custom line."))

        modal = _WarningBlurbModal(
            TEST_GUILD_ID, "ae_plague_marauder", "Alliance Exercise: Plague Marauder", "Old custom"
        )
        modal.field._value = "   "

        inter = MagicMock()
        inter.response.edit_message = AsyncMock()
        await modal.on_submit(inter)

        assert config.get_guild_event(TEST_GUILD_ID, "ae_plague_marauder")["warning_blurb"] == ""
        body = inter.response.edit_message.await_args.kwargs["content"]
        assert WARNING_BLURB_DEFAULT.format(name="Alliance Exercise: Plague Marauder") in body


class TestPickerWarningLine:
    """The one line under an event in the picker.

        5-minute warning: Active - Custom
        5-minute warning: Active - Default
        5-minute warning: Off

    State first. An earlier version put the wording itself here, which
    answered "which one is this" at the cost of "does this one even
    fire" -- and the second question is the live one, because the flag
    genuinely varies per event.
    """

    def test_off_says_off(self):
        from events_hub import _warning_summary

        assert _warning_summary({"five_min_warning": 0}) == "5-minute warning: Off"

    def test_off_says_off_even_with_wording_saved(self):
        """Turning a warning off keeps its wording, so an event can hold
        text that does not post. The line has to lead with the state or it
        would read as active."""
        from events_hub import _warning_summary

        line = _warning_summary({"five_min_warning": 0, "warning_blurb": "Marauder in 5."})
        assert line == "5-minute warning: Off"

    def test_active_with_custom_wording(self):
        from events_hub import _warning_summary

        line = _warning_summary({"five_min_warning": 1, "warning_blurb": "Marauder in 5."})
        assert line == "5-minute warning: Active - Custom"

    def test_active_on_the_default(self):
        from events_hub import _warning_summary

        assert (
            _warning_summary({"five_min_warning": 1, "warning_blurb": ""})
            == "5-minute warning: Active - Default"
        )
        assert _warning_summary({"five_min_warning": 1}) == "5-minute warning: Active - Default"

    def test_whitespace_only_wording_is_not_custom(self):
        from events_hub import _warning_summary

        line = _warning_summary({"five_min_warning": 1, "warning_blurb": "   "})
        assert line == "5-minute warning: Active - Default"

    def test_every_line_fits_discords_description_cap(self):
        """100 characters is Discord's hard cap on a SelectOption
        description; an option over it is rejected by the API."""
        from events_hub import _warning_summary

        for ev in (
            {"five_min_warning": 0},
            {"five_min_warning": 1},
            {"five_min_warning": 1, "warning_blurb": "x" * 5000},
        ):
            assert len(_warning_summary(ev)) <= 100


class TestPerEventWarningToggle:
    """#566: the warning is per event, so an alliance can want one for
    Alliance Exercise and none at all for Glacieradon.

    Before this the only control was `guild_configs.event_five_min_warning`,
    which the wizard copied into each event once at creation. The scheduler
    reads only the per-event column and never that one, so turning the
    server setting off left every existing event still warning.
    """

    def test_setter_turns_a_warning_off_and_back_on(self, temp_db):
        import config

        config.save_guild_event(TEST_GUILD_ID, _base_event_row(five_min_warning=1))

        assert config.set_guild_event_five_min_warning(TEST_GUILD_ID, "ae_plague_marauder", False)
        assert config.get_guild_event(TEST_GUILD_ID, "ae_plague_marauder")["five_min_warning"] == 0

        assert config.set_guild_event_five_min_warning(TEST_GUILD_ID, "ae_plague_marauder", True)
        assert config.get_guild_event(TEST_GUILD_ID, "ae_plague_marauder")["five_min_warning"] == 1

    def test_setter_reports_a_missing_event(self, temp_db):
        import config

        assert (
            config.set_guild_event_five_min_warning(TEST_GUILD_ID, "no_such_event", True) is False
        )

    def test_turning_off_keeps_the_wording(self, temp_db):
        """So turning it back on restores what they wrote, rather than
        silently dropping it. The confirmation copy promises this."""
        import config

        config.save_guild_event(
            TEST_GUILD_ID, _base_event_row(warning_blurb="Marauder in 5. Offline on.")
        )
        config.set_guild_event_five_min_warning(TEST_GUILD_ID, "ae_plague_marauder", False)

        row = config.get_guild_event(TEST_GUILD_ID, "ae_plague_marauder")
        assert row["five_min_warning"] == 0
        assert row["warning_blurb"] == "Marauder in 5. Offline on."

    def test_two_events_can_disagree(self, temp_db):
        """The whole point. One on, one off, in the same alliance."""
        import config

        config.save_guild_event(
            TEST_GUILD_ID,
            _base_event_row(short_key="ae_plague_marauder", five_min_warning=1),
        )
        config.save_guild_event(
            TEST_GUILD_ID,
            _base_event_row(short_key="glacieradon", name="Glacieradon", five_min_warning=0),
        )

        rows = {e["short_key"]: e for e in config.get_guild_events(TEST_GUILD_ID)}
        assert rows["ae_plague_marauder"]["five_min_warning"] == 1
        assert rows["glacieradon"]["five_min_warning"] == 0


class TestSetupCopyDoesNotPromiseAMasterSwitch:
    """`/setup`'s step said "This applies to all events". It never did:
    the scheduler reads each event's own column and never the guild one,
    so turning it off there left every existing event warning."""

    def test_the_false_claim_is_gone(self):
        """Scoped to the warning step. Steps 1 and 2 still say "applies to
        all events" about the draft and announcement channels, and there
        the guild value really is the fallback every event falls back to,
        so the claim holds well enough to leave alone."""
        import inspect

        import setup_cog

        src = inspect.getsource(setup_cog)
        assert "Should the bot automatically post a 5-minute warning before events?" not in src
        assert "Should events you add from now on warn 5 minutes before they start?" in src

    def test_the_scheduler_still_ignores_the_guild_switch(self):
        """Pins the reason the copy changed rather than the code. Making
        the guild field a real master switch would silence every warning
        for an alliance that had only ever turned it off expecting it to
        affect new events, so the copy moved instead."""
        import inspect

        import scheduler

        assert "event_five_min_warning" not in inspect.getsource(scheduler)
