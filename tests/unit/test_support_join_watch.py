"""
Unit tests for the support-server join watch (owner tooling).

Covers:
- app_settings key/value store: get returns default when unset, set persists,
  set(None) deletes.
- shared_bot_guilds: returns only guilds (other than the excluded one) that
  contain the user, sorted by name; uses cache-only get_member.
- format_join_notice: renders the requested message for both the has-overlap
  and the "None" case, and caps the inline list for large overlaps.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import support_join_watch as sjw  # noqa: E402


# ── Fakes ────────────────────────────────────────────────────────────────────


class FakeGuild:
    def __init__(self, gid, name, member_ids):
        self.id = gid
        self.name = name
        self._members = set(member_ids)

    def get_member(self, uid):
        # Cache-only lookup, like discord.Guild.get_member — returns a truthy
        # sentinel if present, None otherwise.
        return object() if uid in self._members else None


class FakeBot:
    def __init__(self, guilds):
        self.guilds = guilds


class FakeMember:
    def __init__(self, uid, name):
        self.id = uid
        self._name = name
        self.mention = f"<@{uid}>"
        self.bot = False

    def __str__(self):
        return self._name


# ── app_settings store ───────────────────────────────────────────────────────


def test_app_setting_default_when_unset(temp_db):
    import config

    assert config.get_app_setting("nope") is None
    assert config.get_app_setting("nope", "fallback") == "fallback"


def test_app_setting_set_get_delete(temp_db):
    import config

    config.set_app_setting(sjw.WATCH_CHANNEL_SETTING, "9876")
    assert config.get_app_setting(sjw.WATCH_CHANNEL_SETTING) == "9876"
    # Overwrite.
    config.set_app_setting(sjw.WATCH_CHANNEL_SETTING, "5555")
    assert config.get_app_setting(sjw.WATCH_CHANNEL_SETTING) == "5555"
    # None deletes.
    config.set_app_setting(sjw.WATCH_CHANNEL_SETTING, None)
    assert config.get_app_setting(sjw.WATCH_CHANNEL_SETTING) is None


# ── shared_bot_guilds ────────────────────────────────────────────────────────


def test_shared_bot_guilds_excludes_home_and_absent():
    bot = FakeBot(
        [
            FakeGuild(100, "Support", [7, 8]),  # excluded (home)
            FakeGuild(1, "Zeta", [7]),
            FakeGuild(2, "Alpha", [7]),
            FakeGuild(3, "Other", [9]),  # user not present
        ]
    )
    shared = sjw.shared_bot_guilds(bot, 7, exclude_guild_id=100)
    # Sorted by name, home + absent excluded.
    assert [g.name for g in shared] == ["Alpha", "Zeta"]


def test_shared_bot_guilds_empty_when_no_overlap():
    bot = FakeBot([FakeGuild(100, "Support", [7]), FakeGuild(1, "Alpha", [8])])
    assert sjw.shared_bot_guilds(bot, 7, exclude_guild_id=100) == []


# ── format_join_notice ───────────────────────────────────────────────────────


def test_format_join_notice_with_overlap():
    m = FakeMember(7, "spammer#1")
    guilds = [FakeGuild(1, "Alpha", [7]), FakeGuild(2, "Beta", [7])]
    msg = sjw.format_join_notice(m, guilds)
    assert "just joined the server" in msg
    assert "Servers they belong to with LW Alliance Helper installed (2): Alpha, Beta" in msg
    assert "**None**" not in msg


def test_format_join_notice_none():
    m = FakeMember(7, "newbie#2")
    msg = sjw.format_join_notice(m, [])
    assert "Servers they belong to with LW Alliance Helper installed: **None**" in msg


def test_format_join_notice_caps_large_lists():
    m = FakeMember(7, "busy#3")
    guilds = [FakeGuild(i, f"G{i:03d}", [7]) for i in range(40)]
    msg = sjw.format_join_notice(m, guilds)
    # Reports the true total but only spells out the cap, plus an overflow note.
    assert "installed (40):" in msg
    assert "more)" in msg


# ── verified_role_blocker ────────────────────────────────────────────────────


def test_verified_role_blocker_ok():
    """Manage Roles present, role below the bot, not managed → assignable."""
    assert (
        sjw.verified_role_blocker(
            has_manage_roles=True,
            bot_top_position=10,
            role_position=5,
            role_managed=False,
        )
        is None
    )


def test_verified_role_blocker_no_manage_roles():
    reason = sjw.verified_role_blocker(
        has_manage_roles=False,
        bot_top_position=10,
        role_position=5,
        role_managed=False,
    )
    assert reason and "Manage Roles" in reason


def test_verified_role_blocker_managed_role():
    reason = sjw.verified_role_blocker(
        has_manage_roles=True,
        bot_top_position=10,
        role_position=5,
        role_managed=True,
    )
    assert reason and "integration" in reason


def test_verified_role_blocker_hierarchy_equal_or_above():
    # Equal position counts as not-below.
    for role_pos in (10, 11):
        reason = sjw.verified_role_blocker(
            has_manage_roles=True,
            bot_top_position=10,
            role_position=role_pos,
            role_managed=False,
        )
        assert reason and "highest role" in reason


def test_verified_role_blocker_permission_checked_before_hierarchy():
    """A bot missing Manage Roles gets the permission reason even when the role
    is also above it — permission is the more fundamental fix."""
    reason = sjw.verified_role_blocker(
        has_manage_roles=False,
        bot_top_position=5,
        role_position=99,
        role_managed=False,
    )
    assert "Manage Roles" in reason
