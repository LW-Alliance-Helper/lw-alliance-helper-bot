"""Regression guard for the messages.py constants extracted in #267.

If a constant gets renamed intentionally, update the literal here too.
The point is to catch *accidental* renames (typo, autoformatter doing
something cute, drift from the audit-locked canonical phrasing) that
would silently propagate to every importing module.

Companion to tests/unit/test_setup_hub_constants.py (which pins the
HUB_BTN_* labels from #208) and tests/unit/test_no_verb_colon_acks.py
(which guards the sentence-form success-ack style rule from D15).
"""
from messages import (
    CANCEL_BACKPEDAL,
    CANCEL_BACKPEDAL_DEFAULT,
    CANCEL_PLAIN,
    DATE_PARSE_REJECT,
    DENY_ADMIN_OR_ROLE,
    DENY_NOT_OWNER,
    FEATURE_NOT_CONFIGURED,
    GENERIC_CMD_TIMEOUT,
    HUB_TIMEOUT,
    INPUT_INVALID,
    INPUT_INVALID_NO_EXAMPLE,
    LEADERSHIP_INACCESSIBLE,
    LEADERSHIP_NO_READ_PERM,
    LEADERSHIP_NOT_CONFIGURED,
    NOT_SET_UP,
    NOT_SET_UP_HUB,
    PREMIUM_LOCKED_INLINE,
    PREV_CHANNEL_GONE,
    SETUP_POINTER_FOOTER,
    TIER_COMPARISON,
    TIME_PARSE_GIVE_UP,
    TIME_PARSE_RETRY,
    WIZARD_TIMEOUT,
)


# ── Wizard scaffolding ───────────────────────────────────────────────

def test_wizard_timeout():
    assert WIZARD_TIMEOUT == "⏰ Timed out. Run `/setup` → {wizard} to start again."


def test_generic_cmd_timeout():
    assert GENERIC_CMD_TIMEOUT == "⏰ Timed out. Run `/{cmd}` to start again."


def test_hub_timeout():
    assert HUB_TIMEOUT == "⏰ Timed out. Run `/{cmd}` and click **{hub_btn}** to start again."


def test_cancel_constants():
    assert CANCEL_PLAIN              == "❌ Cancelled."
    assert CANCEL_BACKPEDAL_DEFAULT  == "↩️ Cancelled. No changes made."
    assert CANCEL_BACKPEDAL          == "↩️ Cancelled. {detail}"


def test_setup_not_complete_constants():
    assert NOT_SET_UP             == "⚙️ This bot hasn't been set up yet. Run `/setup` to get started."
    assert NOT_SET_UP_HUB         == "⚙️ This server hasn't been set up yet. Click **{hub_btn}** above to start."
    assert FEATURE_NOT_CONFIGURED == "⚙️ {feature} isn't configured yet. Run `/setup` → {wizard_btn} first."


# ── Premium / permissions ────────────────────────────────────────────

def test_premium_locked_inline():
    assert PREMIUM_LOCKED_INLINE == "🔒 The **{feature}** is a 💎 Premium feature. Run `/upgrade` to unlock it."


def test_deny_constants():
    assert DENY_NOT_OWNER     == "⛔ Only the user who opened this view can use it."
    assert DENY_ADMIN_OR_ROLE == "⛔ You need server administrator permission or the **{role}** role to {action}."


# ── Leadership / channel errors ──────────────────────────────────────

def test_leadership_constants():
    assert LEADERSHIP_NOT_CONFIGURED == "⚠️ Leadership channel isn't configured. Run `/setup` to configure it."
    assert LEADERSHIP_INACCESSIBLE   == "⚠️ Could not access the leadership channel."
    assert LEADERSHIP_NO_READ_PERM   == "⚠️ Bot does not have permission to read message history in the leadership channel."


def test_prev_channel_gone():
    assert PREV_CHANNEL_GONE == "⚠️ Your previously configured {channel_label} channel no longer exists. Pick a new one below."


# ── Validation ───────────────────────────────────────────────────────

def test_time_parse_constants():
    assert TIME_PARSE_RETRY == (
        "⚠️ Could not read **`{raw}`** as a time. "
        "Try `9:00am`, `10:15pm`, or `22:00`. Let's try once more."
    )
    assert TIME_PARSE_GIVE_UP == (
        "⚠️ Could not read that time after a few tries. "
        "Run {recovery} to start again."
    )


def test_input_invalid_constants():
    assert INPUT_INVALID            == "⚠️ Please enter a {type} like `{example}`. Run {recovery} to try again."
    assert INPUT_INVALID_NO_EXAMPLE == "⚠️ Please enter a {type}. Run {recovery} to try again."


def test_date_parse_reject():
    assert DATE_PARSE_REJECT == "⚠️ `{raw}` isn't a date I can parse. Try {examples}."


# ── Footers ──────────────────────────────────────────────────────────

def test_footer_constants():
    assert TIER_COMPARISON      == "Free tier: {free_limit}. Upgrade to Premium for {premium_limit}."
    assert SETUP_POINTER_FOOTER == "Run `/setup` → {wizard} to update settings."


# ── Templates render without KeyError when called with their documented params ──

def test_templates_format_with_documented_params():
    """Smoke-check that every template's documented parameters actually
    work when formatted. Catches param-name drift between messages.py
    and callsites."""
    assert WIZARD_TIMEOUT.format(wizard="🚂 Train")
    assert GENERIC_CMD_TIMEOUT.format(cmd="setup")
    assert HUB_TIMEOUT.format(cmd="desertstorm", hub_btn="📄 Generate mail")
    assert CANCEL_BACKPEDAL.format(detail="No rule added.")
    assert NOT_SET_UP_HUB.format(hub_btn="⚙️ Open setup wizard")
    assert FEATURE_NOT_CONFIGURED.format(feature="Member Roster Sync", wizard_btn="👥 Member Sync")
    assert PREMIUM_LOCKED_INLINE.format(feature="Trends Viewer")
    assert DENY_ADMIN_OR_ROLE.format(role="Leadership", action="use the setup hub")
    assert PREV_CHANNEL_GONE.format(channel_label="leadership")
    assert TIME_PARSE_RETRY.format(raw="garbage")
    assert TIME_PARSE_GIVE_UP.format(recovery="`/setup` → 🚂 Train")
    assert INPUT_INVALID.format(type="row number", example="2", recovery="`/setup` → 📈 Growth")
    assert INPUT_INVALID_NO_EXAMPLE.format(type="whole number", recovery="`/events`")
    assert DATE_PARSE_REJECT.format(raw="blarg", examples="`May 18`, `5/18`")
    assert TIER_COMPARISON.format(free_limit="7-day window", premium_limit="30 days")
    assert SETUP_POINTER_FOOTER.format(wizard="📈 Growth")
