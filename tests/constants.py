"""
Shared test constants — imported directly by test files.
Kept separate from conftest.py to avoid pytest import path ambiguity.
"""

import os

TEST_GUILD_ID = 1497432945827516639
TEST_SHEET_ID = os.environ.get("TEST_SHEET_ID", "1iRwiwT7-K4jGvqkC1hixNCsNf5twNZ-zDOd8DlZ1kO0")
# Synthetic guild id used by tests that need a "premium-tier" guild —
# distinct from TEST_GUILD_ID so the same test run can exercise both
# free-tier and premium-tier paths without env var collisions. Tests
# that want this guild treated as premium set
# `PREMIUM_BYPASS_GUILD_IDS=<this id>` via monkeypatch (see the
# autouse fixtures in test_dm.py / test_member_roster.py / test_premium.py).
# Not a real Discord snowflake; chosen to be visually obvious as a fixture.
PREMIUM_TEST_GUILD_ID = 999000111222333444
