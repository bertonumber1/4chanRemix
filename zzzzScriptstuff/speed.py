"""
speed.py
========

Performance tiers controlling CPU and I/O scheduling, plus a handful of
related knobs that meaningfully affect bulk-operation throughput.

The naming is a Spaceballs reference (Sub-Light, Light Speed, Ridiculous
Speed, Ludicrous Speed, Plaid). LUDICROUS is the default — it picks the
most aggressive options that don't require root.

What each knob actually does (and what it doesn't):

  nice level
      Linux scheduler priority for CPU time. Range -20..+19; lower is
      higher priority. Negative values require root. For our workload
      (mostly I/O-bound) this has small impact unless the system is
      heavily contended by other processes. We use `os.nice()` which
      silently caps at the user's allowed range, so non-root callers
      won't crash trying to set -20.

  ionice class + level
      Linux block I/O scheduler priority. Three classes:
        RT (1)    realtime — preempts other I/O. Requires root.
        BE (2)    best-effort, our normal class. Within BE, levels 0..7
                  with 0 highest priority.
        IDLE (3)  only runs when the disk is otherwise idle.
      We shell out to the `ionice` binary because Python's stdlib has
      no direct binding (the syscall is `ioprio_set(2)` which we'd
      need ctypes for; shelling out is simpler and just as fast since
      we only call it once at startup).

  SQLite synchronous pragma
      Controls when sqlite calls fsync() to durably commit writes:
        FULL    sync before every commit + before every page write.
                Safest, slowest.
        NORMAL  (sqlite default) sync at critical moments only.
        OFF     never explicitly sync. ~3x faster on bulk inserts but
                loses durability — a system crash mid-import can corrupt
                the journal. For us this is acceptable because the import
                checkpoint lets us resume.
      Quoting the SQLite docs: "With synchronous OFF (0), SQLite
      continues without syncing as soon as it has handed data off to
      the operating system. If the application running SQLite crashes,
      the data will be safe, but the database might become corrupted
      if the operating system crashes or the computer loses power."

  GC pause control
      Python's cyclic garbage collector kicks in periodically and
      pauses the world. For tight import loops we can disable it
      (`gc.disable()`) and reclaim memory manually after each batch.
      Speedup is ~5-10% on bulk imports per the cpython mailing list
      threads on the topic; not huge but free.

  DB transaction batch size
      How many rows we accumulate in memory before opening a single
      transaction and flushing them all. Larger = fewer transactions
      = faster, at the cost of more RAM per batch and bigger rollback
      window on Ctrl+C.

What "ludicrous" deliberately does NOT do:

  - Doesn't try to multi-thread the importer. Mutagen isn't fully
    thread-safe, and our DB has a single connection. Adding threading
    is real work and risk for limited gain on a single-drive workload.

  - Doesn't use io_uring or async syscalls. Python's asyncio doesn't
    integrate well with shutil/mutagen and we'd be rewriting half
    the codebase for marginal benefit.

  - Doesn't override the shutil copy implementation. Python 3.8+
    already uses sendfile(2) under the hood on Linux, which is the
    fastest user-space file copy you can do. Larger buffer sizes
    help only when sendfile isn't being used, which is rare for us.

The settings here move the needle on the right margins. Big claims
like "10x faster" would be lies; expect 1.5-2x improvement under
contention, less on an idle system.
"""

from __future__ import annotations

import gc
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any


logger = logging.getLogger("music-organiser")


# =============================================================================
# TIERS
# =============================================================================

@dataclass(frozen=True)
class SpeedLevel:
    """All performance knobs for one named tier."""
    id: str
    display: str            # the catchy name shown in UI
    description: str        # what this tier promises
    nice: int               # OS nice level, -20..+19
    ionice_class: int       # 1=RT, 2=BE, 3=IDLE
    ionice_level: int       # 0..7 within BE
    sqlite_sync: str        # FULL / NORMAL / OFF
    sqlite_cache_kb: int    # SQLite page cache (kb)
    disable_gc_in_hotloops: bool
    db_batch_size: int      # rows per transaction
    needs_root: bool        # tier requires sudo for full effect
    launch_quote: str       # text shown when this tier kicks off a big op


SPEED_LEVELS: dict[str, SpeedLevel] = {
    "sub-light": SpeedLevel(
        id="sub-light",
        display="Sub-Light",
        description=(
            "Be polite. Run at idle I/O priority so the disk stays free "
            "for whatever else you're doing. Slower but won't slow down "
            "anything else."
        ),
        nice=+10,
        ionice_class=3,             # IDLE — only run when disk is free
        ionice_level=0,
        sqlite_sync="FULL",         # safest
        sqlite_cache_kb=16384,      # 16 MB
        disable_gc_in_hotloops=False,
        db_batch_size=100,
        needs_root=False,
        launch_quote="Cruise control engaged.",
    ),
    "light-speed": SpeedLevel(
        id="light-speed",
        display="Light Speed",
        description=(
            "Stock priority. Comfortable on a desktop you're using "
            "for other things. The conservative default if you can't "
            "have Ludicrous."
        ),
        nice=0,
        ionice_class=2,             # BE
        ionice_level=4,             # middle of best-effort
        sqlite_sync="NORMAL",
        sqlite_cache_kb=32768,      # 32 MB
        disable_gc_in_hotloops=False,
        db_batch_size=250,
        needs_root=False,
        launch_quote="Punch it, Chewie.",
    ),
    "ridiculous": SpeedLevel(
        id="ridiculous",
        display="Ridiculous Speed",
        description=(
            "Aggressive but still cooperative. Best-effort I/O at "
            "priority 2, slightly elevated CPU. Disables GC during "
            "the import hot loop. NORMAL durability."
        ),
        nice=-5,                    # negative needs root; gracefully falls back
        ionice_class=2,
        ionice_level=2,
        sqlite_sync="NORMAL",
        sqlite_cache_kb=65536,      # 64 MB
        disable_gc_in_hotloops=True,
        db_batch_size=500,
        needs_root=False,
        launch_quote="They've gone to Ridiculous Speed.",
    ),
    "ludicrous": SpeedLevel(
        id="ludicrous",
        display="Ludicrous",
        description=(
            "DEFAULT. Best-effort I/O priority 0 (top of the queue). "
            "Negative nice if we can get it. SQLite sync=OFF (faster "
            "bulk inserts; durable via the import checkpoint instead). "
            "GC disabled in hot loops. Big DB batches."
        ),
        nice=-10,
        ionice_class=2,
        ionice_level=0,             # top of best-effort queue
        sqlite_sync="OFF",
        sqlite_cache_kb=131072,     # 128 MB
        disable_gc_in_hotloops=True,
        db_batch_size=2000,
        needs_root=False,           # gracefully falls back if nice fails
        launch_quote="LUDICROUS SPEED! GO!",
    ),
    "plaid": SpeedLevel(
        id="plaid",
        display="Plaid",
        description=(
            "Realtime I/O scheduling (RT class). REQUIRES SUDO. Will "
            "preempt other processes' disk requests. Use this when "
            "the only thing the machine is doing is the import. Falls "
            "back to Ludicrous if we don't have permissions."
        ),
        nice=-15,
        ionice_class=1,             # RT
        ionice_level=0,
        sqlite_sync="OFF",
        sqlite_cache_kb=262144,     # 256 MB
        disable_gc_in_hotloops=True,
        db_batch_size=5000,
        needs_root=True,
        launch_quote="They've gone to plaid.",
    ),
}


# Default tier — picked per the user's spec.
DEFAULT_SPEED = "ludicrous"


# =============================================================================
# APPLY — actually configure the running process
# =============================================================================

@dataclass
class AppliedSpeed:
    """What actually happened when we tried to apply a speed level.
    Includes warnings about pieces that fell back to defaults."""
    level: SpeedLevel
    actual_nice: int            # what nice() actually returned
    actual_ionice: str          # description of what was applied, or "skipped"
    warnings: list[str]


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def _try_set_nice(target: int) -> tuple[int, str | None]:
    """
    Try to lower our nice value to `target`. os.nice() takes a DELTA,
    not an absolute value. We compute the delta from current and apply.

    Returns (final_nice, warning_or_None).
    """
    try:
        current = os.nice(0)
    except (AttributeError, OSError):
        # Windows path — os.nice doesn't exist
        return (0, "nice not available on this platform")

    delta = target - current
    if delta == 0:
        return (current, None)

    try:
        new_val = os.nice(delta)
        if new_val != target:
            return (new_val, f"requested nice={target}, kernel capped at {new_val} (need root for lower)")
        return (new_val, None)
    except PermissionError:
        # We were trying to go negative but lack privilege.
        return (current, f"can't set nice={target} (need root); staying at {current}")
    except OSError as e:
        return (current, f"nice() failed: {e}")


def _try_set_ionice(ionice_class: int, ionice_level: int) -> tuple[str, str | None]:
    """
    Set I/O priority via the `ionice` binary on Linux. Returns
    (description, warning_or_None).

    Class meanings:
      1 = RT (realtime). Needs root or CAP_SYS_NICE.
      2 = BE (best-effort). Default, no special privileges.
      3 = IDLE. No special privileges.
    """
    if not is_linux():
        return ("not Linux — skipped", None)
    if shutil.which("ionice") is None:
        return ("`ionice` binary not found", "install the `util-linux` package to use ionice")

    cls_name = {1: "RT", 2: "BE", 3: "IDLE"}.get(ionice_class, "?")
    cmd = ["ionice", "-c", str(ionice_class), "-n", str(ionice_level),
           "-p", str(os.getpid())]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        return ("(timed out)", "ionice took too long")
    except FileNotFoundError:
        return ("(missing)", "ionice not on PATH")

    if r.returncode != 0:
        # Most common cause: trying RT without root.
        msg = (r.stderr or r.stdout or "").strip()
        return (f"failed: {cls_name}:{ionice_level}", f"ionice rejected: {msg}")
    return (f"{cls_name}:{ionice_level}", None)


def apply_speed(level_id: str | None = None) -> AppliedSpeed:
    """
    Apply a speed level to the running process. Returns AppliedSpeed
    describing what actually took effect.

    Idempotent in the sense that calling it twice with the same level
    is safe (you can re-nice yourself, ionice yourself, etc.).
    """
    if not level_id:
        level_id = DEFAULT_SPEED
    if level_id not in SPEED_LEVELS:
        logger.warning("unknown speed level %r — using %s", level_id, DEFAULT_SPEED)
        level_id = DEFAULT_SPEED

    level = SPEED_LEVELS[level_id]
    warnings: list[str] = []

    # --- CPU priority (nice) -----
    actual_nice, nice_warn = _try_set_nice(level.nice)
    if nice_warn:
        warnings.append(nice_warn)

    # --- I/O priority (ionice) -----
    # For Plaid (RT class) we try first, fall back to Ludicrous BE:0 if rejected.
    actual_ionice, io_warn = _try_set_ionice(level.ionice_class, level.ionice_level)
    if io_warn and level.ionice_class == 1:
        warnings.append(f"plaid downgrade: {io_warn}")
        # Fall back to ludicrous best-effort 0
        actual_ionice, io_warn2 = _try_set_ionice(2, 0)
        warnings.append("downgraded to BE:0 (ludicrous-equivalent)")
        if io_warn2:
            warnings.append(io_warn2)
    elif io_warn:
        warnings.append(io_warn)

    # --- GC -----
    if level.disable_gc_in_hotloops:
        # We don't actually disable here — that's per-hotloop. We just
        # note that hot loops SHOULD disable. The actual gc.disable()
        # calls happen at the start of importer/indexer/etc loops.
        pass

    applied = AppliedSpeed(
        level=level,
        actual_nice=actual_nice,
        actual_ionice=actual_ionice,
        warnings=warnings,
    )
    # Record what's active so get_active_level() / hot_loop() / batch size
    # helpers find it. Previously this wasn't happening and downstream
    # callers saw None — hot_loop became a no-op even in Ludicrous mode,
    # and Database picked up baseline pragmas instead of the tuned ones.
    set_active_level(level)
    return applied


# =============================================================================
# CONTEXT MANAGER FOR HOT LOOPS
# =============================================================================

class hot_loop:
    """
    Context manager for code paths that should temporarily disable GC
    if the active speed level says so. Usage:

        from speed import get_active_level, hot_loop
        with hot_loop(get_active_level()):
            for f in many_files:
                process(f)

    On exit, GC is re-enabled and one collection is forced to clean up
    accumulated garbage from the loop body.
    """

    def __init__(self, level: SpeedLevel | None):
        self.level = level
        self.was_enabled: bool = True

    def __enter__(self):
        if self.level and self.level.disable_gc_in_hotloops:
            self.was_enabled = gc.isenabled()
            gc.disable()
        return self

    def __exit__(self, *exc):
        if self.level and self.level.disable_gc_in_hotloops:
            if self.was_enabled:
                gc.enable()
            try:
                gc.collect()
            except Exception:
                pass
        return False


# =============================================================================
# ACTIVE-LEVEL TRACKING
# =============================================================================
#
# So callers can query "what tier is active right now" without
# threading the SpeedLevel through every function signature. Set once
# at startup by organiser.main(); read anywhere via get_active_level().

_ACTIVE_LEVEL: SpeedLevel | None = None


def set_active_level(level: SpeedLevel) -> None:
    global _ACTIVE_LEVEL
    _ACTIVE_LEVEL = level


def get_active_level() -> SpeedLevel | None:
    return _ACTIVE_LEVEL


def get_active_sqlite_pragmas() -> dict[str, Any]:
    """Pragma dict to feed Database(..., pragmas=...)."""
    lvl = _ACTIVE_LEVEL or SPEED_LEVELS[DEFAULT_SPEED]
    return {
        "synchronous": lvl.sqlite_sync,
        "cache_size_kb": lvl.sqlite_cache_kb,
    }


def get_active_batch_size() -> int:
    lvl = _ACTIVE_LEVEL or SPEED_LEVELS[DEFAULT_SPEED]
    return lvl.db_batch_size


def get_launch_quote() -> str:
    """Punchline shown in the activity log when a big operation
    begins. The Ludicrous one is the GO! reference."""
    lvl = _ACTIVE_LEVEL or SPEED_LEVELS[DEFAULT_SPEED]
    return lvl.launch_quote
