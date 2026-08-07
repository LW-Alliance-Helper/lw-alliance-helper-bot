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


class TestPlanPost:
    """Over half of releases land within 24h of the previous one, so a
    burst shares one message instead of firing a notification each time."""

    WINDOW = 12 * 3600
    NOW = 1_000_000.0

    def _state(self, age_seconds, content="**1.7.5** — 2026-07-30\n- earlier"):
        return {
            "message_id": "123",
            "posted_at": self.NOW - age_seconds,
            "content": content,
        }

    def _plan(self, block, state, **kw):
        from scripts.discord_changelog import plan_post

        return plan_post(
            block, state, now=self.NOW, window_seconds=kw.pop("window", self.WINDOW), **kw
        )

    def test_first_release_posts_a_new_message(self):
        plan = self._plan("**1.8.0** — 2026-08-01\n- a thing", None)
        assert plan["action"] == "post"
        assert plan["content"] == "**1.8.0** — 2026-08-01\n- a thing"

    def test_a_release_inside_the_window_appends_to_the_last_message(self):
        plan = self._plan("**1.7.6** — 2026-07-30\n- a fix", self._state(2 * 3600))
        assert plan["action"] == "patch"
        assert plan["message_id"] == "123"
        assert plan["content"].startswith("**1.7.5**")
        assert plan["content"].endswith("- a fix")
        assert "\n\n**1.7.6**" in plan["content"]

    def test_a_release_after_the_window_starts_a_new_message(self):
        plan = self._plan("**1.8.0** — 2026-08-01\n- a thing", self._state(13 * 3600))
        assert plan["action"] == "post"
        assert plan["content"] == "**1.8.0** — 2026-08-01\n- a thing"

    def test_appending_past_discord_s_limit_starts_a_new_message(self):
        """Better a second message than a truncated one."""
        plan = self._plan(
            "**1.7.6** — 2026-07-30\n- a fix",
            self._state(1 * 3600, content="x" * 1990),
        )
        assert plan["action"] == "post"

    def test_lost_state_starts_a_new_message_rather_than_dropping_the_release(self):
        for state in (None, {}, {"message_id": "", "posted_at": 0, "content": ""}):
            assert self._plan("**1.8.0** — d\n- x", state)["action"] == "post"

    def test_a_clock_skewed_future_timestamp_does_not_append(self):
        assert self._plan("**1.8.0** — d\n- x", self._state(-500))["action"] == "post"

    def test_nothing_to_say_plans_nothing(self):
        assert self._plan(None, self._state(60))["action"] == "none"
        assert self._plan("**1.9.0** — d\nNO POST: quiet", None)["action"] == "none"

    def test_the_burst_window_is_configurable(self):
        block = "**1.7.6** — 2026-07-30\n- a fix"
        assert self._plan(block, self._state(5 * 3600), window=3600)["action"] == "post"
        assert self._plan(block, self._state(5 * 3600), window=6 * 3600)["action"] == "patch"


class TestPlanCli:
    def test_plan_emits_json_the_workflow_can_read(self, tmp_path, capsys):
        import json

        path = tmp_path / "d.md"
        path.write_text(FILE, encoding="utf-8")
        code = main(["1.8.4", "--path", str(path), "--plan"])
        plan = json.loads(capsys.readouterr().out)

        assert code == 0
        assert plan["action"] == "post"
        assert plan["content"].startswith("**1.8.4**")

    def test_an_unreadable_state_file_still_plans_a_post(self, tmp_path, capsys):
        import json

        path = tmp_path / "d.md"
        path.write_text(FILE, encoding="utf-8")
        state = tmp_path / "state.json"
        state.write_text("not json at all", encoding="utf-8")

        code = main(["1.8.4", "--path", str(path), "--plan", "--state", str(state)])
        out = capsys.readouterr()

        assert code == 0
        assert json.loads(out.out)["action"] == "post"
        assert "No usable post state" in out.err

    def test_a_stored_recent_message_plans_an_append(self, tmp_path, capsys):
        import json, time

        path = tmp_path / "d.md"
        path.write_text(FILE, encoding="utf-8")
        state = tmp_path / "state.json"
        state.write_text(
            json.dumps(
                {
                    "message_id": "999",
                    "posted_at": time.time() - 600,
                    "content": "**1.8.3** — d\n- x",
                }
            ),
            encoding="utf-8",
        )

        main(["1.8.4", "--path", str(path), "--plan", "--state", str(state)])
        plan = json.loads(capsys.readouterr().out)

        assert plan["action"] == "patch"
        assert plan["message_id"] == "999"


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
