# Vulture whitelist: names that look unused to the tool and are not.
#
# Vulture rates every unused function, method, class and variable at a flat
# 60% confidence (only unused imports at 90 and unreachable code at 100), so
# the working band is 60 and this file is what makes 60 readable. Grow it:
# every run that proves a name live adds it here, in the section that says why.
#
# Format is vulture's own (`--make-whitelist`), one line per NAME: vulture
# matches whitelist entries by name, so one `_.on_submit` covers every modal.
# Run:
#   python -m vulture . .vulture-whitelist.py --min-confidence 60 --exclude ".venv,tests,assets,scripts"
# or scripts/quality/dead_code.py, which adds the repo-wide reference check.
#
# Started by the code-dead-code skill run of 2026-09-09 on champion_duel_*.py;
# regenerated 2026-09-12 after the base views (#594) took the pasted
# `interaction_check` / `on_timeout` methods and the dead-code pass (#589,
# step 9) removed what nothing reached.

# 1. Framework-dispatched: discord.py or aiohttp calls these by convention,
#    decorator or route table, never by a call site in this repo.
_.on_submit  # discord.ui.Modal
_.keep  # @discord.ui.button
_.use_mine  # @discord.ui.button
_.timeout_hint  # read by wizard_registry.ExpiringView.on_timeout
_.disabled  # discord.ui.Item state, set on children before an edit
_.row_factory  # sqlite3.Connection
setup  # champion_duel_cog.py: discord.py cog entry point
preflight  # api/champion_duel_auth.py: aiohttp route
login  # api/champion_duel_auth.py: aiohttp route
exchange  # api/champion_duel_auth.py: aiohttp route
me  # api/champion_duel_auth.py: aiohttp route
logout  # api/champion_duel_auth.py: aiohttp route
requires_session  # api/champion_duel_auth.py: decorator applied by the routes
requires_writer  # api/champion_duel_auth.py: decorator applied by the routes
requires_admin  # api/champion_duel_auth.py: decorator applied by the routes

# 2. Called from outside the champion_duel_* family (bot.py, bot_admin.py,
#    config.py, api/, scripts/), which a family-scoped scan cannot see.
due  # champion_duel_store.py
ensure_grouping  # champion_duel_db.py
find_grouping_conflicts  # champion_duel_db.py
get_roster  # champion_duel_db.py
import_orders  # champion_duel_db.py
import_profiles  # champion_duel_db.py
import_registrants  # champion_duel_db.py
import_squads  # champion_duel_db.py
init_db  # champion_duel_db.py
init_store  # champion_duel_store.py
merge_groupings  # champion_duel_db.py
picks_size  # champion_duel_image.py
purge_guild_data  # champion_duel_db.py
purge_user_data  # champion_duel_db.py
record_import  # champion_duel_db.py
revoke_guild_sessions  # champion_duel_db.py
run_one  # champion_duel_store.py

# 3. Documented keeps: the code's own comment, or the log on #589, says why.
entry_position  # champion_duel_picks.py: carried for the card's order
claims_for  # champion_duel_db.py: waits for the claim marker on the listings, per champion_duel_claim.py's docstring
build  # champion_duel_picks.py: the stored-card reader the picks tests score through
list_disagreements  # champion_duel_db.py: the read side of the disagreement log; the hub tests look through it
list_imports  # champion_duel_db.py: the read side of the import log; the import tests look through it
READS_CHAR_BUDGET  # champion_duel_hub.py: the yardstick the read-size test measures against
_embed_chars  # champion_duel_hub.py: how that test measures
W  # champion_duel_image.py: the canvas the image tests measure every box against
H  # champion_duel_image.py: the same
team_reads  # champion_duel_hub.py: in progress, touched 2026-09-07
