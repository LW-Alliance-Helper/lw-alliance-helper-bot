"""volume_health.py — what the Railway volume is holding, and an alarm on it.

## The failure this exists for

The Railway volume is a thin-provisioned ZFS zvol. It allocates blocks on
write and does not hand them back when a file is deleted: the guest
filesystem frees the bytes, the host keeps the blocks. SQLite's default
rollback journal creates and deletes ``guild_configs.db-journal`` on *every*
write transaction, and six ``@tasks.loop(minutes=1)`` loops stamp
``loop_heartbeat`` continuously — so that create/delete cycle ran all day and
the volume's *reported* usage climbed ~55 MB/day on a filesystem holding
under 2 MB. It reached 3.7 GB of a 5 GB volume before anyone looked.
``fstrim`` cannot reclaim it; the container is refused the FITRIM ioctl.

``PRAGMA journal_mode=WAL`` in :func:`config._get_conn` is the fix: one
``-wal`` file, appended and checkpointed in place, so blocks are reused
instead of freshly allocated.

## Why the alarm watches the cause, not the symptom

The obvious alarm — "tell me when the volume passes 60%" — cannot work from
in here. The over-allocation is a property of the *host's* zvol and is
invisible to the container: ``df`` reports the couple of megabytes the
filesystem actually holds while Railway's graph climbs into the gigabytes.
An in-container threshold on ``shutil.disk_usage`` would have stayed silent
through the entire incident.

So :func:`check` alarms on the **cause** instead, which is precisely
observable from here: a database on the volume whose ``journal_mode`` is not
``wal``, or a ``-journal`` file sitting on the volume at all. Both are one
cheap read, and both catch the regression the moment a new database ships
without the setting or an old one is restored from a non-WAL backup.

The filesystem threshold is kept as a second, independent check. It will not
catch the zvol problem, but it does catch the ordinary one — a volume
genuinely filling with data — and the two need different fixes.

Reading Railway's own volume metric would need their GraphQL API and a
token; that is deliberately out of scope.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3

from discord.ext import commands, tasks

import config

logger = logging.getLogger(__name__)

# Alarm when the *filesystem* passes this. Not the zvol — see the module
# docstring for why that one cannot be measured from inside the container.
FILESYSTEM_ALERT_PERCENT = int(os.getenv("VOLUME_ALERT_PERCENT", "75"))

# Six hours. Both conditions are durable state, not moments: a journal_mode
# regression persists until someone ships a fix, and a filling disk fills
# over days. A tighter loop would re-read the same PRAGMAs all day.
CHECK_INTERVAL_HOURS = 6

# Where the alarm posts. An env var rather than a DB row on purpose: the
# volume is exactly what this alarm distrusts, and CHANGELOG_CHANNEL_ID
# already set the precedent that deploy config survives a volume reset.
ALERT_CHANNEL_ENV = "OPS_ALERT_CHANNEL_ID"

DB_SUFFIXES = (".db", ".sqlite", ".sqlite3")


def volume_dir() -> str:
    """The directory the volume is mounted at, derived from the configured
    database path so the two can never drift apart."""
    return os.path.dirname(config.DB_PATH) or "."


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024 or unit == "TB":
            return f"{size:,.0f} {unit}" if unit == "B" else f"{size:,.1f} {unit}"
        size /= 1024
    return f"{size:,.1f} TB"


def list_files(directory: str) -> list[tuple[str, int]]:
    """Every file on the volume with its size, largest first.

    Deliberately includes the ``-wal`` / ``-shm`` / ``-journal`` siblings.
    A ``-journal`` file present at rest is direct evidence the rollback
    journal is live, which is the whole failure in one directory listing.
    """
    out: list[tuple[str, int]] = []
    for root, _dirs, names in os.walk(directory):
        for name in sorted(names):
            path = os.path.join(root, name)
            try:
                out.append((path, os.path.getsize(path)))
            except OSError:
                continue
    return sorted(out, key=lambda pair: -pair[1])


def database_paths(directory: str) -> list[str]:
    return [p for p, _ in list_files(directory) if p.endswith(DB_SUFFIXES)]


def database_stats(path: str) -> dict:
    """Page and journal facts for one database, read-only.

    Opened with ``mode=ro`` so a diagnostic can never write to the thing it
    is diagnosing. A WAL database can refuse a read-only open when it needs
    to create its ``-shm``, so that falls back to a normal connection with
    ``query_only`` set — still no writes, just a less absolute guarantee.
    """
    stats: dict = {"path": path, "error": None, "tables": [], "dbstat": False}
    try:
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.OperationalError:
            conn = sqlite3.connect(path)
        conn.execute("PRAGMA query_only = ON")
    except sqlite3.Error as exc:
        stats["error"] = str(exc)
        return stats

    try:
        for pragma in ("journal_mode", "page_size", "page_count", "freelist_count"):
            stats[pragma] = conn.execute(f"PRAGMA {pragma}").fetchone()[0]
        stats["file_bytes"] = stats["page_size"] * stats["page_count"]
        stats["free_bytes"] = stats["page_size"] * stats["freelist_count"]

        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        owner = dict(conn.execute("SELECT name, tbl_name FROM sqlite_master"))

        # dbstat is the accurate answer (real page usage, indexes included)
        # but it is a compile-time option: present on Debian's SQLite, absent
        # from some builds. Fall back to summing column lengths, which
        # undercounts index and page overhead but still ranks the tables
        # correctly, which is all this view is for.
        try:
            rows = conn.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name").fetchall()
            rolled: dict[str, int] = {}
            for name, size in rows:
                key = owner.get(name, name)
                rolled[key] = rolled.get(key, 0) + (size or 0)
            stats["tables"] = sorted(rolled.items(), key=lambda kv: -kv[1])
            stats["dbstat"] = True
        except sqlite3.OperationalError:
            est: list[tuple[str, int]] = []
            for table in tables:
                cols = [c[1] for c in conn.execute(f'PRAGMA table_info("{table}")')]
                expr = " + ".join(f'COALESCE(LENGTH("{c}"),0)' for c in cols) or "0"
                size = conn.execute(f'SELECT COALESCE(SUM({expr}),0) FROM "{table}"').fetchone()[0]
                est.append((table, size or 0))
            stats["tables"] = sorted(est, key=lambda kv: -kv[1])
    except sqlite3.Error as exc:
        stats["error"] = str(exc)
    finally:
        conn.close()
    return stats


def journal_offenders(directory: str | None = None) -> list[str]:
    """Databases on the volume that are NOT in WAL — the alarm condition."""
    directory = directory or volume_dir()
    bad = []
    for path in database_paths(directory):
        stats = database_stats(path)
        if stats.get("error"):
            continue
        if str(stats.get("journal_mode", "")).lower() != "wal":
            bad.append(path)
    return bad


def rollback_journals(directory: str | None = None) -> list[str]:
    """Any ``-journal`` file on the volume. Present at rest means the
    rollback journal is in use right now, whatever the PRAGMA says."""
    directory = directory or volume_dir()
    return [p for p, _ in list_files(directory) if p.endswith("-journal")]


def check(directory: str | None = None) -> list[str]:
    """Return the alarm lines that currently apply. Empty means healthy."""
    directory = directory or volume_dir()
    alarms: list[str] = []

    try:
        offenders = journal_offenders(directory)
        if offenders:
            names = ", ".join(f"`{os.path.basename(p)}`" for p in offenders)
            alarms.append(
                f"🚨 **journal_mode regression** — {names} is not in WAL. Every "
                "write transaction burns volume the host will not give back. "
                "See `volume_health.py` for the history."
            )
        strays = rollback_journals(directory)
        if strays:
            names = ", ".join(f"`{os.path.basename(p)}`" for p in strays)
            alarms.append(
                f"🚨 **Rollback journal on the volume** — {names}. The "
                "create/delete cycle that climbs ~55 MB/day is running now."
            )
    except OSError as exc:
        logger.warning("[VOLUME] could not inspect %s: %s", directory, exc)

    try:
        usage = shutil.disk_usage(directory)
        pct = usage.used / usage.total * 100 if usage.total else 0
        if pct >= FILESYSTEM_ALERT_PERCENT:
            alarms.append(
                f"⚠️ **Filesystem at {pct:.0f}%** — {human(usage.used)} of "
                f"{human(usage.total)}. This is real data, not zvol drift."
            )
    except OSError:
        pass
    return alarms


def format_report(directory: str | None = None, max_tables: int = 8) -> str:
    """The whole picture as one Discord-ready block."""
    directory = directory or volume_dir()
    lines = [f"**Volume** `{directory}`", ""]

    files = list_files(directory)
    if not files:
        lines.append("_No files — is the volume mounted here?_")
        return "\n".join(lines)

    total = sum(size for _, size in files)
    lines.append("**Files**")
    for path, size in files:
        lines.append(f"`{human(size):>9}`  {os.path.basename(path)}")
    lines.append(f"`{human(total):>9}`  **total**")

    try:
        usage = shutil.disk_usage(directory)
        pct = usage.used / usage.total * 100 if usage.total else 0
        lines += [
            "",
            f"**Filesystem** {human(usage.used)} of {human(usage.total)} ({pct:.1f}%)",
            "_Railway's volume graph measures the host's zvol and will read_",
            "_higher than this. The gap is unreturned blocks, not data._",
        ]
    except OSError:
        pass

    for path in database_paths(directory):
        stats = database_stats(path)
        lines += ["", f"**{os.path.basename(path)}**"]
        if stats["error"]:
            lines.append(f"⚠️ unreadable: `{stats['error']}`")
            continue
        free_pct = stats["freelist_count"] / stats["page_count"] * 100 if stats["page_count"] else 0
        lines.append(
            f"`journal_mode={stats['journal_mode']}` · "
            f"{human(stats['file_bytes'])} in {stats['page_count']:,} pages · "
            f"{human(stats['free_bytes'])} free ({free_pct:.1f}%)"
        )
        if str(stats["journal_mode"]).lower() != "wal":
            lines.append(
                "🚨 **Not WAL.** Every write transaction is creating and "
                "deleting a `-journal` file, and the zvol never takes those "
                "blocks back. This is the ~55 MB/day failure."
            )
        if free_pct > 20:
            lines.append(
                f"⚠️ {free_pct:.0f}% of the file is free pages — deleted rows, "
                "not live data. `VACUUM` would reclaim it inside the file."
            )
        if stats["tables"]:
            basis = "measured" if stats["dbstat"] else "estimated, content only"
            lines.append(f"_Largest tables ({basis}):_")
            for name, size in stats["tables"][:max_tables]:
                lines.append(f"`{human(size):>9}`  {name}")
    return "\n".join(lines)


async def alert_target(bot):
    """The channel to shout into, or the application owner as a fallback so
    an unset env var degrades to "still reaches someone" rather than silence."""
    raw = os.environ.get(ALERT_CHANNEL_ENV, "").strip()
    if raw.isdigit():
        channel = bot.get_channel(int(raw))
        if channel is not None:
            return channel
    try:
        app = await bot.application_info()
        return app.owner
    except Exception:  # noqa: BLE001 - alerting must never raise
        return None


class VolumeHealthCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._last_alarms: list[str] = []
        self.watch.start()

    def cog_unload(self):
        self.watch.cancel()

    @tasks.loop(hours=CHECK_INTERVAL_HOURS)
    async def watch(self):
        try:
            alarms = check()
        except Exception as e:  # noqa: BLE001 - the watchdog must not die
            logger.warning("[VOLUME] check failed: %s", e)
            return

        # Only speak on a change. These conditions are durable, so repeating
        # the same alarm every six hours trains the reader to ignore it.
        if alarms and alarms != self._last_alarms:
            target = await alert_target(self.bot)
            if target is not None:
                try:
                    await target.send("\n".join(alarms))
                except Exception as e:  # noqa: BLE001
                    logger.warning("[VOLUME] could not deliver alarm: %s", e)
            else:
                logger.warning("[VOLUME] alarm with nowhere to send it: %s", alarms)
        self._last_alarms = alarms

        if alarms:
            logger.warning("[VOLUME] %s", " | ".join(alarms))
        config.stamp_loop_heartbeat("volume_health")

    @watch.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(VolumeHealthCog(bot))
