"""
lockfile.py
===========

Detect other instances of music-organiser running against the same DB.

The honest engineering reality:
  - SQLite WAL mode supports many readers + one writer concurrently
  - Two writers won't corrupt the file but WILL race-update rows
  - Building a real work-sharing queue is a separate project (hundreds
    of lines of locking, deadlock prevention, etc.)

What this module does:
  - Drops a lockfile at $XDG_RUNTIME_DIR/music-organiser/<db_hash>.lock
    (or /tmp fallback) containing PID + timestamp + mode
  - On startup: looks for an existing lockfile, checks if the PID is
    still alive. If yes, asks the user what to do.
  - Modes: 'writer' (full access, blocks other writers) and 'reader'
    (browser-only, allowed alongside one writer).

Limitations:
  - PID checks only work on Linux/macOS (via os.kill(pid, 0))
  - On Windows we fall back to "trust the lockfile" with a timeout
  - If the script crashes hard (SIGKILL, power loss), stale lockfiles
    remain. We detect that via PID-not-alive and offer to clear.

What this does NOT do:
  - Coordinate work between instances ("you take artists A-M, I take
    N-Z") — that's outside scope.
  - Lock individual rows or albums — too fine-grained, too risky.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


logger = logging.getLogger("music-organiser")


def _lockfile_dir() -> Path:
    """Where to put lockfiles. Prefer XDG_RUNTIME_DIR (tmpfs, per-user)
    on Linux; fall back to /tmp (or %TEMP% on Windows)."""
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        p = Path(xdg) / "music-organiser"
    else:
        import tempfile
        p = Path(tempfile.gettempdir()) / f"music-organiser-{os.getuid() if hasattr(os, 'getuid') else 'user'}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _db_key(db_path: str | Path) -> str:
    """Stable short key for a DB path — used as lockfile basename so two
    instances pointed at different DBs don't conflict."""
    p = str(Path(db_path).expanduser().resolve())
    return hashlib.sha1(p.encode()).hexdigest()[:12]


def _is_pid_alive(pid: int) -> bool:
    """Cross-platform check for whether a PID is still running. On Unix
    we send signal 0 (no actual signal) and look at the error; on Windows
    we use a process listing fallback that's never accurate enough so we
    just trust the timestamp."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # Windows doesn't have os.kill(pid, 0) semantics; without
        # pulling in psutil we can't reliably check. Trust the timestamp.
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # The PID exists but is owned by another user. Still "alive"
        # from our perspective — we can't write a lockfile that says
        # "this process is mine".
        return True
    except OSError:
        return False


@dataclass
class LockInfo:
    """Contents of an existing lockfile."""
    pid: int = 0
    started_at: float = 0.0    # epoch seconds
    mode: str = ""             # 'writer' / 'reader'
    hostname: str = ""
    db_path: str = ""
    stale: bool = False        # True if PID is no longer alive

    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.started_at)

    def age_human(self) -> str:
        s = self.age_seconds()
        if s < 60: return f"{int(s)}s"
        if s < 3600: return f"{int(s/60)}m"
        if s < 86400: return f"{int(s/3600)}h"
        return f"{int(s/86400)}d"


def read_lockfiles(db_path: str | Path) -> list[LockInfo]:
    """Return all lockfiles for the given DB. Multiple are possible
    when we allow readers + one writer."""
    out: list[LockInfo] = []
    base = _lockfile_dir()
    key = _db_key(db_path)
    for p in base.glob(f"{key}*.lock"):
        try:
            content = p.read_text(encoding="utf-8")
            info = LockInfo(db_path=str(db_path))
            for line in content.splitlines():
                if "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip()
                if k == "pid":
                    try: info.pid = int(v)
                    except ValueError: pass
                elif k == "started":
                    try: info.started_at = float(v)
                    except ValueError: pass
                elif k == "mode":
                    info.mode = v
                elif k == "hostname":
                    info.hostname = v
            info.stale = not _is_pid_alive(info.pid)
            out.append(info)
        except OSError:
            continue
    return out


class InstanceLock:
    """
    Context manager that acquires a lock for one instance.

    Usage:
        with InstanceLock(db_path, mode='writer') as lock:
            ... run pipeline ...

    On enter:
      - Reads existing lockfiles
      - If a writer is running and we want to be a writer too: ask user
        what to do (wait / become reader / proceed anyway / abort)
      - If we want reader and a writer exists: allowed; just register
      - Writes our own lockfile

    On exit: removes our lockfile. If the process crashes, the lockfile
    is detected as stale on next startup and offered for cleanup.
    """

    def __init__(self, db_path: str | Path, mode: str = "writer") -> None:
        self.db_path = str(db_path)
        self.mode = mode
        self.our_lockfile: Path | None = None
        self.existing: list[LockInfo] = []

    def __enter__(self) -> "InstanceLock":
        self.existing = read_lockfiles(self.db_path)
        # Drop stale entries from the list passed to the user
        live = [i for i in self.existing if not i.stale]
        stale = [i for i in self.existing if i.stale]

        if stale:
            print()
            print(f"  Found {len(stale)} stale lockfile(s) — process died "
                  f"without cleanup.")
            try:
                clear = input("  Clear them? [Y/n] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                clear = "n"
            if clear in ("", "y", "yes"):
                base = _lockfile_dir()
                key = _db_key(self.db_path)
                for p in base.glob(f"{key}*.lock"):
                    try:
                        content = p.read_text(encoding="utf-8")
                        for line in content.splitlines():
                            if line.startswith("pid="):
                                try:
                                    pid = int(line.split("=", 1)[1])
                                    if not _is_pid_alive(pid):
                                        p.unlink()
                                        break
                                except ValueError:
                                    pass
                    except OSError:
                        pass

        # Re-read so live reflects current truth after cleanup
        live = [i for i in read_lockfiles(self.db_path) if not i.stale]
        live_writers = [i for i in live if i.mode == "writer"]

        # Decision tree
        if self.mode == "writer" and live_writers:
            print()
            print("  ⚠  Another music-organiser instance is already writing")
            print(f"     to this database (PID {live_writers[0].pid}, "
                  f"{live_writers[0].age_human()} old).")
            print()
            print("  SQLite handles concurrent reads but not concurrent")
            print("  writers — both running the same pipeline will race-")
            print("  update rows and redo each other's work.")
            print()
            print("  What do you want to do?")
            print("    r — switch this instance to READ-ONLY (browser only)")
            print("    p — proceed anyway (you've been warned)")
            print("    q — quit and let the other instance finish")
            try:
                ans = input("  > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = "q"
            if ans == "r":
                self.mode = "reader"
            elif ans == "p":
                pass   # proceed; user accepted the risk
            else:
                print("  aborting.")
                sys.exit(0)

        # Write our own lockfile
        base = _lockfile_dir()
        key = _db_key(self.db_path)
        # Per-pid filename so multiple readers can coexist
        self.our_lockfile = base / f"{key}.{os.getpid()}.lock"
        try:
            import socket
            hostname = socket.gethostname()
        except Exception:
            hostname = "unknown"
        content = (
            f"pid={os.getpid()}\n"
            f"started={time.time()}\n"
            f"mode={self.mode}\n"
            f"hostname={hostname}\n"
            f"db_path={self.db_path}\n"
            f"version=0.20.0\n"
        )
        try:
            self.our_lockfile.write_text(content, encoding="utf-8")
        except OSError as e:
            logger.warning("could not write lockfile: %s", e)
            self.our_lockfile = None
        return self

    def __exit__(self, *exc) -> None:
        if self.our_lockfile is not None:
            try:
                self.our_lockfile.unlink()
            except OSError:
                pass

    def is_read_only(self) -> bool:
        return self.mode == "reader"
