"""
Unit tests for scripts/discord_changelog.py (#92).

The workflow posts whatever this prints, so an empty result is the
"say nothing" signal and every failure path has to reach it without
raising. A crash here would go red on main, and Railway's "Wait for CI
checks" would then block the production deploy for a missing Discord post.
"""

import pytest
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from scripts.discord_changelog import find_block, main

FILE = """# Discord changelog posts

Preamble that must never be posted.

---

**1.8.4** — 2026-08-07
- Adding the app without the bot now says so

---

**1.8.10** — 2026-08-06
- A later patch

---

**1.7.1 to 1.7.4** — 2026-07-21
- 1.7.1 to 1.7.3 were support tooling, nothing alliance-facing
"""


class TestFindBlock:
    def test_finds_the_block_for_a_version(self):
        block = find_block(FILE, "1.8.4")
        assert block.startswith("**1.8.4** — 2026-08-07")
        assert "Adding the app without the bot now says so" in block

    def test_never_returns_the_preamble(self):
        """It's the first block in the file and explains the format."""
        for version in ("1.8.4", "1.7.4", "9.9.9"):
            assert "Preamble" not in (find_block(FILE, version) or "")

    def test_a_range_header_matches_every_version_it_names(self):
        """A quiet release posts nothing, then the next one covers the gap."""
        for version in ("1.7.1", "1.7.4"):
            assert find_block(FILE, version).startswith("**1.7.1 to 1.7.4**")

    def test_a_missing_version_returns_none(self):
        assert find_block(FILE, "2.0.0") is None

    def test_an_empty_version_returns_none(self):
        assert find_block(FILE, "") is None

    def test_a_shorter_version_does_not_match_inside_a_longer_one(self):
        """1.8.1 must not match the 1.8.10 header."""
        assert find_block(FILE, "1.8.1") is None

    def test_blocks_do_not_bleed_into_each_other(self):
        assert "1.8.10" not in find_block(FILE, "1.8.4")


class TestMain:
    def _run(self, tmp_path, capsys, content, version, extra=None):
        path = tmp_path / "DISCORD_CHANGELOG.md"
        path.write_text(content, encoding="utf-8")
        code = main([version, "--path", str(path), *(extra or [])])
        return code, capsys.readouterr()

    def test_prints_the_block_and_exits_zero(self, tmp_path, capsys):
        code, out = self._run(tmp_path, capsys, FILE, "1.8.4")
        assert code == 0
        assert out.out.strip().startswith("**1.8.4**")

    def test_a_missing_block_prints_nothing_and_still_exits_zero(self, tmp_path, capsys):
        code, out = self._run(tmp_path, capsys, FILE, "2.0.0")
        assert code == 0
        assert out.out.strip() == ""
        assert "No block for 2.0.0" in out.err

    def test_a_missing_file_prints_nothing_and_still_exits_zero(self, tmp_path, capsys):
        code = main(["1.8.4", "--path", str(tmp_path / "nope.md")])
        assert code == 0
        assert capsys.readouterr().out.strip() == ""

    def test_an_over_long_block_is_refused_rather_than_truncated(self, tmp_path, capsys):
        """Discord caps content at 2000 chars. Half a changelog is worse
        than none, so it declines and says so."""
        long_block = "---\n\n**1.9.0** — 2026-09-01\n" + ("- padding padding\n" * 200)
        code, out = self._run(tmp_path, capsys, long_block, "1.9.0")
        assert code == 0
        assert out.out.strip() == ""
        assert "over the 2000 limit" in out.err

    def test_a_block_at_the_limit_still_posts(self, tmp_path, capsys):
        block = "---\n\n**1.9.0** — 2026-09-01\n- ok"
        code, out = self._run(tmp_path, capsys, block, "1.9.0", extra=["--limit", "40"])
        assert code == 0
        assert out.out.strip().startswith("**1.9.0**")


class TestNoPost:
    """Skipping a release is explicit, so a hole always means forgotten."""

    SKIPPED = """---

**1.9.0** — 2026-09-01
NO POST: dependency bumps only
"""

    def test_a_marked_block_sends_nothing(self, tmp_path, capsys):
        from scripts.discord_changelog import is_no_post

        path = tmp_path / "d.md"
        path.write_text(self.SKIPPED, encoding="utf-8")
        code = main(["1.9.0", "--path", str(path)])
        out = capsys.readouterr()

        assert code == 0
        assert out.out.strip() == ""
        assert "NO POST" in out.err
        assert is_no_post(find_block(self.SKIPPED, "1.9.0"))

    def test_a_marked_block_still_satisfies_the_release_check(self, tmp_path, capsys):
        path = tmp_path / "d.md"
        path.write_text(self.SKIPPED, encoding="utf-8")
        assert main(["1.9.0", "--path", str(path), "--check"]) == 0

    def test_a_normal_block_is_not_treated_as_skipped(self):
        from scripts.discord_changelog import is_no_post

        assert not is_no_post(find_block(FILE, "1.8.4"))

    def test_the_marker_is_only_honoured_in_the_body(self):
        """A header mentioning it must not silence a real post."""
        from scripts.discord_changelog import is_no_post

        assert not is_no_post("**1.9.0** — NO POST era\n- a real change")


class TestCheckMode:
    """Gate on the release PR — this is what makes 'every release posts' true."""

    def test_a_missing_block_fails_the_release_pr(self, tmp_path, capsys):
        path = tmp_path / "d.md"
        path.write_text(FILE, encoding="utf-8")
        code = main(["2.0.0", "--path", str(path), "--check"])
        err = capsys.readouterr().err

        assert code == 1
        assert "No Discord changelog block for 2.0.0" in err
        assert "NO POST" in err, "the failure must show how to opt out"

    def test_a_present_block_passes(self, tmp_path, capsys):
        path = tmp_path / "d.md"
        path.write_text(FILE, encoding="utf-8")
        assert main(["1.8.4", "--path", str(path), "--check"]) == 0

    def test_an_over_long_block_fails_before_it_reaches_main(self, tmp_path, capsys):
        """Better to fail the PR than to warn after the release shipped."""
        path = tmp_path / "d.md"
        path.write_text("---\n\n**1.9.0** — 2026-09-01\n" + ("- pad\n" * 400), encoding="utf-8")
        code = main(["1.9.0", "--path", str(path), "--check"])

        assert code == 1
        assert "over Discord's" in capsys.readouterr().err

    def test_a_missing_file_fails_the_check(self, tmp_path):
        assert main(["1.9.0", "--path", str(tmp_path / "nope.md"), "--check"]) == 1


class TestShippedFile:
    """The real file has to parse, or the first release using this posts
    nothing and nobody finds out until the channel stays quiet."""

    def _content(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(root, "docs", "DISCORD_CHANGELOG.md"), encoding="utf-8") as f:
            return f.read()

    @pytest.mark.parametrize("version", ["1.8.1", "1.8.2", "1.8.3", "1.8.4"])
    def test_every_seeded_release_resolves(self, version):
        block = find_block(self._content(), version)
        assert block is not None, f"no block for {version}"
        assert block.splitlines()[0].startswith(f"**{version}**")

    @pytest.mark.parametrize("version", ["1.8.1", "1.8.2", "1.8.3", "1.8.4"])
    def test_every_block_fits_in_a_discord_message(self, version):
        assert len(find_block(self._content(), version)) <= 2000

    def test_the_preamble_is_never_posted(self):
        assert "Discord changelog posts" not in (find_block(self._content(), "1.8.4") or "")
