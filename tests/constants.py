"""
Shared test constants — imported directly by test files.
Kept separate from conftest.py to avoid pytest import path ambiguity.
"""
import os

TEST_GUILD_ID = 1497432945827516639
TEST_SHEET_ID = os.environ.get(
    "TEST_SHEET_ID",
    "1iRwiwT7-K4jGvqkC1hixNCsNf5twNZ-zDOd8DlZ1kO0"
)
OGV_GUILD_ID  = 1266229297723605052
