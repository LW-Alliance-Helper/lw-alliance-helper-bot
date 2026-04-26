"""
Shared fixtures for sheet integration tests.
Creates and cleans up real worksheets in the test spreadsheet.
"""
import pytest
import random
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


TEST_SHEET_ID = os.environ.get(
    "TEST_SHEET_ID",
    "1iRwiwT7-K4jGvqkC1hixNCsNf5twNZ-zDOd8DlZ1kO0"
)

# Standard tab names the test suite creates/uses
TAB_SQUAD_POWERS  = "_test_squad_powers"
TAB_HISTORY       = "_test_survey_history"
TAB_GROWTH        = "_test_growth_tracking"
TAB_DS_ASSIGNMENTS= "_test_ds_assignments"
TAB_CS_ASSIGNMENTS= "_test_cs_assignments"
TAB_TRAIN         = "_test_train_schedule"

ALL_TEST_TABS = [
    TAB_SQUAD_POWERS, TAB_HISTORY, TAB_GROWTH,
    TAB_DS_ASSIGNMENTS, TAB_CS_ASSIGNMENTS, TAB_TRAIN,
]


@pytest.fixture(scope="session")
def gc(sheets_client):
    """Alias for the gspread client from the root conftest."""
    return sheets_client


@pytest.fixture(scope="session")
def sh(test_spreadsheet):
    """Alias for the test spreadsheet from the root conftest."""
    return test_spreadsheet


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_tabs(test_spreadsheet):
    """
    Before the session: delete any leftover test tabs from previous runs.
    After the session: delete all test tabs created during this run.
    """
    def delete_test_tabs(spreadsheet):
        for ws in spreadsheet.worksheets():
            if ws.title.startswith("_test_"):
                try:
                    spreadsheet.del_worksheet(ws)
                except Exception:
                    pass

    delete_test_tabs(test_spreadsheet)
    yield
    delete_test_tabs(test_spreadsheet)


@pytest.fixture
def fresh_tab(test_spreadsheet):
    """Create a uniquely named test tab, yield it, delete after test."""
    name = f"_test_{random.randint(100000, 999999)}"
    ws   = test_spreadsheet.add_worksheet(title=name, rows=200, cols=30)
    yield ws
    try:
        test_spreadsheet.del_worksheet(ws)
    except Exception:
        pass


@pytest.fixture
def squad_powers_tab(test_spreadsheet):
    """Create a Squad Powers test tab, yield it, delete after test."""
    name = f"_test_sq_{random.randint(100000, 999999)}"
    ws   = test_spreadsheet.add_worksheet(title=name, rows=200, cols=30)
    yield ws, name
    try:
        test_spreadsheet.del_worksheet(ws)
    except Exception:
        pass


@pytest.fixture
def history_tab(test_spreadsheet):
    """Create a Survey History test tab, yield it, delete after test."""
    name = f"_test_hist_{random.randint(100000, 999999)}"
    ws   = test_spreadsheet.add_worksheet(title=name, rows=200, cols=30)
    yield ws, name
    try:
        test_spreadsheet.del_worksheet(ws)
    except Exception:
        pass


@pytest.fixture
def growth_tab(test_spreadsheet):
    """Create a Growth Tracking test tab, yield it, delete after test."""
    name = f"_test_growth_{random.randint(100000, 999999)}"
    ws   = test_spreadsheet.add_worksheet(title=name, rows=200, cols=50)
    yield ws, name
    try:
        test_spreadsheet.del_worksheet(ws)
    except Exception:
        pass
