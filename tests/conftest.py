"""
Shared fixtures for all tests.
Provides: temp DB, mock guild config, mock Discord objects, sheet client.
"""
import os
import sys
import json
import sqlite3
import tempfile
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from dataclasses import dataclass
from typing import Optional

import pytest
import pytest_asyncio

# ── Add project root to path ───────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Test constants (also importable from tests.constants) ─────────────────────
from tests.constants import TEST_GUILD_ID, TEST_SHEET_ID, PREMIUM_TEST_GUILD_ID


# ── FORCE_PREMIUM lane gating ─────────────────────────────────────────────────
#
# A subset of our tests asserts free-tier behavior (caps, locked features,
# upsell embeds). When CI runs the `FORCE_PREMIUM=1` lane, every guild
# resolves as premium, so those tests can't possibly pass and should be
# skipped instead of failed.
#
# Mark such tests with `@pytest.mark.free_tier_only`.

def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "free_tier_only: skip when FORCE_PREMIUM=1 (test asserts free-tier behavior).",
    )


def pytest_collection_modifyitems(config, items):
    if os.environ.get("FORCE_PREMIUM", "").strip().lower() in {"1", "true", "yes"}:
        skip = pytest.mark.skip(reason="FORCE_PREMIUM=1 — every guild is premium")
        for item in items:
            if "free_tier_only" in item.keywords:
                item.add_marker(skip)


# ── Temp database fixture ──────────────────────────────────────────────────────
@pytest.fixture(scope="function")
def temp_db(tmp_path, monkeypatch):
    """
    Create a fresh temporary SQLite database for each test.
    Patches config.DB_PATH and config._get_conn so all config
    functions use the temp DB.
    """
    db_path = str(tmp_path / "test_guild_configs.db")

    import config

    # Patch DB_PATH first
    monkeypatch.setattr(config, "DB_PATH", db_path)

    # Patch _get_conn to use the temp path with row_factory
    def patched_get_conn():
        conn = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(config, "_get_conn", patched_get_conn)

    # Now init_db will use our patched _get_conn
    config.init_db()

    yield db_path


@pytest.fixture(scope="function")
def seeded_db(temp_db, monkeypatch):
    """
    temp_db with the test guild seeded with realistic defaults.
    """
    import config

    cfg = config.get_or_create_config(TEST_GUILD_ID)
    cfg.member_role_name         = "Member"
    cfg.leadership_role_name     = "Leadership"
    cfg.leadership_channel_id    = 111111111111111111
    cfg.announcement_channel_id  = 222222222222222222
    cfg.survey_channel_id        = 333333333333333333
    cfg.survey_notify_channel_id = 444444444444444444
    cfg.timezone                 = "America/New_York"
    cfg.spreadsheet_id           = TEST_SHEET_ID
    cfg.setup_complete           = True
    config.save_config(cfg)

    yield temp_db


# ── Mock Discord objects ───────────────────────────────────────────────────────
def make_mock_user(user_id: int = 123456789, display_name: str = "TestUser",
                   is_admin: bool = True):
    user = MagicMock()
    user.id           = user_id
    user.display_name = display_name
    user.name         = display_name
    user.guild_permissions = MagicMock()
    user.guild_permissions.administrator = is_admin
    return user


def make_mock_role(role_id: int = 987654321, name: str = "TestRole"):
    role = MagicMock()
    role.id   = role_id
    role.name = name
    return role


def make_mock_channel(channel_id: int = 111111111111111111, name: str = "test-channel"):
    channel          = AsyncMock()
    channel.id       = channel_id
    channel.name     = name
    channel.category_id = None
    channel.send     = AsyncMock(return_value=MagicMock(id=999))
    channel.history  = AsyncMock(return_value=[])
    return channel


def make_mock_guild(guild_id: int = TEST_GUILD_ID, name: str = "Test Alliance"):
    guild      = MagicMock()
    guild.id   = guild_id
    guild.name = name
    return guild


def make_mock_interaction(guild_id: int = TEST_GUILD_ID,
                          user_id: int = 123456789,
                          channel_id: int = 111111111111111111,
                          is_admin: bool = True):
    interaction                = AsyncMock()
    interaction.guild_id       = guild_id
    interaction.user           = make_mock_user(user_id, is_admin=is_admin)
    interaction.channel        = make_mock_channel(channel_id)
    interaction.guild          = make_mock_guild(guild_id)
    interaction.response       = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer        = AsyncMock()
    interaction.followup              = AsyncMock()
    interaction.followup.send         = AsyncMock()
    return interaction


@pytest.fixture
def mock_interaction():
    return make_mock_interaction()


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    bot.guilds = [make_mock_guild()]
    bot.get_channel = MagicMock(return_value=make_mock_channel())
    bot.wait_for    = AsyncMock()
    return bot


# ── Google Sheets client fixture ───────────────────────────────────────────────
@pytest.fixture(scope="session")
def sheets_client():
    """
    Real gspread client using service account credentials.

    Resolves the credentials in the same order the bot does:
      1. GOOGLE_CREDENTIALS_JSON env var (raw JSON string)
      2. GOOGLE_SERVICE_ACCOUNT_FILE env var (path to a JSON file)
      3. ./service_account.json relative to the project root

    Skipped when none of those resolve to readable credentials.
    """
    import gspread
    from google.oauth2.service_account import Credentials

    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]

    creds = None

    # 1. Raw-JSON env var (preferred for CI)
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        try:
            info  = json.loads(creds_json)
            creds = Credentials.from_service_account_info(info, scopes=scopes)
        except Exception as e:
            pytest.skip(f"GOOGLE_CREDENTIALS_JSON couldn't be parsed: {e}")

    # 2 & 3. File path env var, or default ./service_account.json
    if creds is None:
        # Project root is two directories up from tests/conftest.py
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        key_file = os.environ.get(
            "GOOGLE_SERVICE_ACCOUNT_FILE",
            os.path.join(project_root, "service_account.json"),
        )
        if os.path.isfile(key_file):
            try:
                creds = Credentials.from_service_account_file(key_file, scopes=scopes)
            except Exception as e:
                pytest.skip(f"Could not load creds from {key_file}: {e}")

    if creds is None:
        pytest.skip(
            "No Google service-account credentials available. Set "
            "GOOGLE_CREDENTIALS_JSON or GOOGLE_SERVICE_ACCOUNT_FILE, or "
            "drop a service_account.json file at the project root."
        )

    try:
        return gspread.authorize(creds)
    except Exception as e:
        pytest.skip(f"Could not create sheets client: {e}")


@pytest.fixture(scope="session")
def test_spreadsheet(sheets_client):
    """Open the test spreadsheet."""
    try:
        return sheets_client.open_by_key(TEST_SHEET_ID)
    except Exception as e:
        pytest.skip(f"Could not open test spreadsheet: {e}")


@pytest.fixture
def test_worksheet(test_spreadsheet):
    """
    Create a fresh worksheet for a single test. The tab persists past
    the test — see `tests/sheets/conftest.py::cleanup_test_tabs` for
    the policy: scrub at the START of every session, leave intact at
    the END so a human can inspect what was written.
    """
    import random
    tab_name = f"_test_{random.randint(10000, 99999)}"
    ws = test_spreadsheet.add_worksheet(title=tab_name, rows=100, cols=30)
    yield ws  # Best effort cleanup


# ── Async event loop ───────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
