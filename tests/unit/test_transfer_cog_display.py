"""Status-value display (#16): checkbox TRUE/FALSE renders as Yes/No in
leadership-facing copy, other status text passes through. The bot still writes
the literal TRUE/FALSE booleans the checkbox needs; only the display maps.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

import transfer_cog


class TestDisplayStatusValue:
    def test_true_false_become_yes_no(self):
        assert transfer_cog._display_status_value("TRUE") == "Yes"
        assert transfer_cog._display_status_value("false") == "No"
        assert transfer_cog._display_status_value(" True ") == "Yes"

    def test_text_passes_through(self):
        assert transfer_cog._display_status_value("Confirmed") == "Confirmed"

    def test_blank_and_none(self):
        assert transfer_cog._display_status_value("") == "(blank)"
        assert transfer_cog._display_status_value(None) == "(blank)"


class TestStatusChangeWording:
    def test_each_change_reads_from_to(self):
        embed = transfer_cog._status_change_embed(
            "Bob", [("Confirmed", "FALSE", "TRUE"), ("Want?", "", "TRUE")]
        )
        assert "**Confirmed** has changed from No to Yes" in embed.description
        assert "**Want?** has changed from (blank) to Yes" in embed.description
