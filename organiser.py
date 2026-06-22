#!/usr/bin/env python3
"""
organiser.py
============

Entry point. Run this:

    python organiser.py

What happens on first run:
  1. Checks Python version (>= 3.11).
  2. Auto-installs missing Python deps (mutagen, rich, tomli_w) using
     pacman if it's available (Arch), else pip --user.
  3. Detects the lack of a config file and launches the first-run wizard.
  4. Drops you into the main menu.

Subsequent runs skip steps 2-3.

The dep bootstrap MUST run before we import any module that needs an
optional package (mutagen lives behind a try/except so it's safe to
import; rich and tomli_w are also optional).
"""

from __future__ import annotations

# Bump this whenever a user-visible bug-fix or feature lands. Shown
# briefly at startup so you can tell which build you're running.
__version__ = "0.23.29"

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


# =============================================================================
# LAYOUT BOOTSTRAP — find our sibling modules
# =============================================================================
#
# Modules can live in two layouts:
#   (1) FLAT: all .py files next to organiser.py (legacy, still supported)
#   (2) NESTED: modules tucked into a subfolder like ./scriptstuff/ so the
#       library root stays clean. The folder can have any name; we look
#       for known marker files (ui.py is a good canary).
#
# Why two layouts: the user shares their music library on Soulseek and
# wants the root folder to look tidy. Burying the 23 .py files into one
# subfolder makes the library visually cleaner.
#
# We prepend the found folder onto sys.path so `from ui import ...` etc.
# resolve to the nested version without changing any import statements
# elsewhere in the codebase. sys.path-mucking is mildly gross but it's
# the lowest-risk option — turning the codebase into a true Python
# package would mean rewriting hundreds of import lines.

_ENTRY_DIR = Path(__file__).resolve().parent
_KNOWN_SCRIPT_FOLDERS = [
    "scriptstuff",          # canonical default
    "zzzzScriptstuff",      # the user's preferred name (sorts to bottom)
    "zzzScriptstuff",       # variant
    "_organiser",
    "music_organiser",
    ".scripts",
]

def _find_modules_dir() -> Path | None:
    """
    Return the path to a sibling folder containing our modules, or None
    if everything's in flat layout next to organiser.py.

    Strategy:
      1. Look for any of _KNOWN_SCRIPT_FOLDERS in our entry dir.
      2. Failing that, scan every immediate subdirectory for one that
         contains a `ui.py` (our reliable marker).
      3. Return None to mean "use flat layout".
    """
    # If flat layout already has ui.py next to organiser.py, prefer that
    # — avoids surprising users who haven't migrated.
    if (_ENTRY_DIR / "ui.py").exists() and (_ENTRY_DIR / "database.py").exists():
        return None
    # Look at known folder names first
    for name in _KNOWN_SCRIPT_FOLDERS:
        candidate = _ENTRY_DIR / name
        if candidate.is_dir() and (candidate / "ui.py").exists():
            return candidate
    # Last resort: scan immediate subdirs for one with ui.py
    try:
        for child in sorted(_ENTRY_DIR.iterdir()):
            if not child.is_dir():
                continue
            # Skip obvious music-library folders (no ui.py inside)
            if (child / "ui.py").exists() and (child / "database.py").exists():
                return child
    except OSError:
        pass
    return None


_MODULES_DIR = _find_modules_dir()
if _MODULES_DIR is not None:
    # Prepend so our modules win over any system-installed packages
    # with the same name (unlikely but possible — 'audit', 'metadata',
    # 'browser' are common names).
    sys.path.insert(0, str(_MODULES_DIR))


# =============================================================================
# DEP BOOTSTRAP — must run before importing our own modules that need them
# =============================================================================

# Each entry: (friendly_name, python_import_name, pacman_package, pip_package)
REQUIRED_DEPS = [
    ("mutagen",  "mutagen",  "python-mutagen",  "mutagen"),
    ("rich",     "rich",     "python-rich",     "rich"),
    ("tomli_w",  "tomli_w",  "python-tomli-w",  "tomli_w"),
    ("pyfiglet", "pyfiglet", "python-pyfiglet", "pyfiglet"),
]


def _module_available(name: str) -> bool:
    """Cheap check: is the module importable without actually running it?"""
    import importlib.util
    return importlib.util.find_spec(name) is not None


def _has_pacman() -> bool:
    return shutil.which("pacman") is not None


def _has_sudo() -> bool:
    return shutil.which("sudo") is not None


def _pip_install(pkg: str) -> bool:
    """
    Install via pip. Modern Arch's Python is PEP 668 marked so pip will
    refuse to install into the system Python without --break-system-packages.
    We prefer --user (per-user site-packages) which sidesteps PEP 668
    on most setups, but fall back to --break-system-packages --user if
    that fails.
    """
    cmds = [
        [sys.executable, "-m", "pip", "install", "--user", pkg],
        [sys.executable, "-m", "pip", "install", "--user",
         "--break-system-packages", pkg],
    ]
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, check=False)
            if r.returncode == 0:
                return True
        except Exception:
            continue
    return False


def _pacman_install(pkg: str) -> bool:
    """
    Try pacman, with sudo if available. Returns True on success.
    """
    if not _has_pacman():
        return False
    cmd_base = ["pacman", "-S", "--noconfirm", "--needed", pkg]
    if _has_sudo():
        cmd = ["sudo"] + cmd_base
    else:
        # Already root? Try direct.
        if os.geteuid() != 0:
            print(f"  (skipping pacman for {pkg} — not root and no sudo)")
            return False
        cmd = cmd_base
    try:
        r = subprocess.run(cmd, check=False)
        return r.returncode == 0
    except Exception:
        return False


def _check_writable(cfg: dict) -> bool:
    """
    Return True if writes are allowed, False if running in read-only
    mode (another writer instance holds the lock). Commands that mutate
    DB/files should call this and bail early if False.
    """
    runtime = cfg.get("_runtime", {})
    if runtime.get("read_only"):
        print()
        print("  ✗ This instance is running in READ-ONLY mode.")
        print("    Another music-organiser process holds the write lock.")
        print("    Wait for it to finish, or run only browse/audit commands here.")
        return False
    return True


def _ask_yn(prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    try:
        ans = input(f"{prompt} ({d}) ").strip().lower()
    except EOFError:
        return default
    if not ans:
        return default
    return ans in ("y", "yes")


class _QuitMenu(Exception):
    """Sentinel used by dispatch handlers to ask main_menu to exit."""
    pass


# =============================================================================
# CONFIG-ON-DEMAND
# =============================================================================
#
# Many features need config values (API keys, paths, options) that may
# not have been set up yet. Rather than crashing or silently using
# defaults, we ask the user at the moment we need the value, save the
# answer to config, and return it.
#
# Usage:
#     token = cfg_ask(cfg, "providers.discogs.token", default="",
#                     prompt="Discogs API token",
#                     description="Get one at https://...",
#                     sensitive=True)
#
# The dotted key path navigates into nested dicts, creating sections
# as needed.

def cfg_get_nested(cfg: dict, dotted_key: str, default: Any = None) -> Any:
    """Get a value from a nested dict via 'a.b.c' syntax."""
    parts = dotted_key.split(".")
    cur: Any = cfg
    for p in parts:
        if not isinstance(cur, dict):
            return default
        if p not in cur:
            return default
        cur = cur[p]
    return cur


def cfg_set_nested(cfg: dict, dotted_key: str, value: Any) -> None:
    """Set a value into a nested dict via 'a.b.c' syntax, creating sections."""
    parts = dotted_key.split(".")
    cur: dict = cfg
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value


def cfg_ask(
    cfg: dict,
    key: str,
    *,
    default: str = "",
    prompt: str = "",
    description: str = "",
    sensitive: bool = False,
    persist: bool = True,
) -> str:
    """
    Ensure a config value exists. Return it, asking the user if absent.

    - If the value is already in config, return it.
    - Otherwise, print `description` (multiline ok), prompt with
      `prompt`, accept user input (or empty for default), and — if
      `persist` is True — save back to config.

    `sensitive=True` hints that the value is a credential. We don't
    currently echo-suppress (no getpass) but we annotate the prompt and
    omit the default from view, so a glance at the terminal doesn't
    leak it via the default hint.
    """
    existing = cfg_get_nested(cfg, key)
    if existing is not None and existing != "":
        return str(existing)

    print()
    if description:
        for line in description.splitlines():
            print(f"  {line}")
    label = prompt or key
    if sensitive:
        hint = "" if not default else " [press enter to skip]"
    else:
        hint = "" if not default else f" [{default}]"
    try:
        raw = input(f"  {label}{hint}: ").strip()
    except (EOFError, KeyboardInterrupt):
        raw = ""

    value = raw if raw else default
    if persist and value:
        cfg_set_nested(cfg, key, value)
        try:
            from config import save_config
            save_config(cfg)
            _log("cfg_ask: saved %s to config", key)
        except Exception as e:
            _log_exc("cfg_ask: save_config failed for %s", key)
    return value


# =============================================================================
# LAST-USED CHOICES — per-command saved defaults
# =============================================================================
#
# Many commands ask a sequence of questions. After the user answers
# once, defaulting the same questions to those answers on the next run
# saves keystrokes and matches the user's mental model ("the same as
# last time"). The choices land under [last_used.<command_id>] in
# config.toml — namespaced so two commands' "providers" don't clash.
#
# Foundation for future "named profiles": once last_used works, we can
# expose multiple named buckets ([profiles.daily], [profiles.deep_dig])
# with no further plumbing. Not building the UI for that today — wait
# until the user actually wants a second profile.

def save_last_used(cfg: dict, command_id: str, key: str, value: Any) -> None:
    """
    Persist a user choice for next time. command_id namespaces it
    (e.g. 'fetch_metadata', 'organise', 'rebuild') so commands don't
    collide on shared key names.

    Writes synchronously to config.toml. If the write fails (read-only
    FS, etc), logs but doesn't raise — saving last-used is a nice-to-have,
    not critical.
    """
    full_key = f"last_used.{command_id}.{key}"
    try:
        cfg_set_nested(cfg, full_key, value)
        from config import save_config
        save_config(cfg)
        _log("save_last_used: %s = %r", full_key, value)
    except Exception:
        _log_exc("save_last_used: failed for %s", full_key)


def load_last_used(cfg: dict, command_id: str, key: str, fallback: Any = None) -> Any:
    """Read a last-used value. Returns fallback if absent."""
    full_key = f"last_used.{command_id}.{key}"
    val = cfg_get_nested(cfg, full_key)
    if val is None:
        return fallback
    return val


def ask_yn_with_last_used(
    cfg: dict, command_id: str, key: str, prompt: str, default: bool = True
) -> bool:
    """
    Like _ask_yn but defaults to whatever the user picked last time
    for the same (command_id, key) pair. Persists the new answer.
    """
    saved = load_last_used(cfg, command_id, key, fallback=default)
    answer = _ask_yn(prompt, default=bool(saved))
    save_last_used(cfg, command_id, key, answer)
    return answer


# =============================================================================
# DEBUG LOG
# =============================================================================
#
# Everything goes to ~/.cache/music-organiser/debug.log so we can post-mortem
# weird behaviour like "menu option did nothing". The log is plain text,
# rotates by truncating to last ~200KB on startup, and every entry is a
# single timestamped line.
#
# Use it like: _log("menu choice received: %r", choice)

import logging as _logging

_DEBUG_LOG_PATH: Path | None = None
_DEBUG_LOGGER: "_logging.Logger | None" = None


def _setup_debug_log() -> None:
    """Initialise the debug log. Called once at startup."""
    global _DEBUG_LOG_PATH, _DEBUG_LOGGER
    if _DEBUG_LOGGER is not None:
        return

    # XDG_CACHE_HOME or ~/.cache
    cache_dir = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "music-organiser"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Fall back to /tmp if we can't write to ~/.cache.
        cache_dir = Path("/tmp/music-organiser")
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return  # No log; give up silently.

    log_path = cache_dir / "debug.log"

    # Truncate if it's grown past 200KB. Keep last 100KB.
    try:
        if log_path.exists() and log_path.stat().st_size > 200_000:
            with open(log_path, "rb") as fp:
                fp.seek(-100_000, os.SEEK_END)
                tail = fp.read()
            with open(log_path, "wb") as fp:
                fp.write(b"--- log truncated ---\n")
                fp.write(tail)
    except OSError:
        pass

    logger = _logging.getLogger("music-organiser")
    logger.setLevel(_logging.DEBUG)
    logger.propagate = False
    # Drop any pre-existing handlers (defensive — re-entering this fn).
    for h in list(logger.handlers):
        logger.removeHandler(h)
    try:
        handler = _logging.FileHandler(log_path, mode="a", encoding="utf-8")
        handler.setFormatter(_logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        logger.addHandler(handler)
    except OSError:
        return

    _DEBUG_LOG_PATH = log_path
    _DEBUG_LOGGER = logger
    logger.info("=== music-organiser v%s startup ===", __version__)


def _log(msg: str, *args: Any, level: int = _logging.DEBUG) -> None:
    """Write a line to the debug log. No-op if log isn't initialised."""
    if _DEBUG_LOGGER is None:
        return
    try:
        _DEBUG_LOGGER.log(level, msg, *args)
    except Exception:
        pass


def _log_exc(msg: str, *args: Any) -> None:
    """Write a line + exception traceback to the debug log."""
    if _DEBUG_LOGGER is None:
        return
    try:
        _DEBUG_LOGGER.exception(msg, *args)
    except Exception:
        pass


def get_debug_log_path() -> Path | None:
    """Where is the debug log? Used by 'show config' and the post-mortem hint."""
    return _DEBUG_LOG_PATH


# =============================================================================
# UNIVERSAL SELECTION HELPER
# =============================================================================
#
# Many places in the UI need "pick one of these labels". This helper
# accepts a number, a full name, a case-insensitive name, a unique
# prefix, a single letter (if unique to one entry), or an unambiguous
# substring. Disambiguation logic is the same everywhere so behaviour is
# predictable.

def resolve_selection(
    choice: str,
    items: list[str],
) -> tuple[str | None, list[str]]:
    """
    Resolve a user's free-form selection against a list of canonical
    names. Returns (resolved_name_or_None, ambiguous_candidates).

    - If `choice` is empty → (None, [])
    - If `choice` is a number → look up by 1-based index; if out of
      range, return (None, []).
    - If `choice` matches one item exactly (case-insensitive) → that one.
    - If `choice` is a unique prefix → that one.
    - If `choice` is a unique substring → that one.
    - If `choice` matches multiple → (None, [those matches])
    - If no matches → (None, [])

    Callers print the appropriate error themselves.
    """
    c = choice.strip()
    if not c:
        return (None, [])

    # Numeric
    if c.isdigit():
        try:
            idx = int(c)
            if 1 <= idx <= len(items):
                return (items[idx - 1], [])
            return (None, [])
        except ValueError:
            pass

    cl = c.lower()

    # Exact match (case-insensitive)
    exact = [it for it in items if it.lower() == cl]
    if len(exact) == 1:
        return (exact[0], [])

    # Prefix match (case-insensitive)
    prefix = [it for it in items if it.lower().startswith(cl)]
    if len(prefix) == 1:
        return (prefix[0], [])
    if len(prefix) > 1:
        return (None, prefix)

    # Substring match (case-insensitive)
    substr = [it for it in items if cl in it.lower()]
    if len(substr) == 1:
        return (substr[0], [])
    if len(substr) > 1:
        return (None, substr)

    return (None, [])


def select_from(
    items: list[str],
    prompt: str = "  > ",
    *,
    current: str | None = None,
    allow_empty: bool = False,
) -> str | None:
    """
    Repeatedly prompt until the user picks an item or cancels (q / empty).

    Returns the chosen item, or None if the user cancelled.
    Accepts: number, name, prefix, substring (see resolve_selection).

    Special commands inside this picker:
      q / quit / empty (when allow_empty=False) → cancel, return None
    """
    while True:
        try:
            raw = input(prompt)
        except (EOFError, KeyboardInterrupt):
            return None
        choice = raw.strip()
        _log("select_from prompt=%r got=%r", prompt, choice)
        if not choice:
            if allow_empty:
                return None
            print("  (type a name, number, q to cancel)")
            continue
        if choice.lower() in ("q", "quit", "exit", "back"):
            return None

        resolved, ambig = resolve_selection(choice, items)
        if resolved is not None:
            _log("select_from resolved %r -> %r", choice, resolved)
            return resolved

        if ambig:
            print(f"  '{choice}' is ambiguous — could be any of:")
            for m in ambig[:10]:
                print(f"    {m}")
            if len(ambig) > 10:
                print(f"    ...and {len(ambig) - 10} more. Type more chars to narrow.")
            continue

        # No match
        print(f"  no item matches '{choice}'. Type a number 1-{len(items)}, a name, or q to cancel.")


def bootstrap_dependencies(install_mode: str = "prompt") -> None:
    """
    Ensure REQUIRED_DEPS are importable. install_mode:
      'prompt' — ask before installing each
      'auto'   — install all silently
      'never'  — exit with error if missing

    Uses platform_detect to figure out the right install command for
    the host OS — works on Arch, Debian, Fedora, openSUSE, Alpine,
    macOS (via Homebrew), NixOS (via pip), FreeBSD, etc. Falls through
    to pip --user when there's no system package available.
    """
    missing = [
        (name, imp, pac, pip_pkg)
        for (name, imp, pac, pip_pkg) in REQUIRED_DEPS
        if not _module_available(imp)
    ]
    if not missing:
        return

    print()
    print(f"Missing Python packages: {', '.join(m[0] for m in missing)}")

    if install_mode == "never":
        print("install_mode = 'never'; please install them manually and re-run.")
        sys.exit(1)

    # Detect host OS so we can recommend the right command.
    try:
        from platform_detect import detect_os, install_command
        os_info = detect_os()
        print(f"Detected OS: {os_info.pretty_name} (pkg manager: {os_info.family})")
    except Exception as e:
        # platform_detect import failed somehow — fall back to old logic
        _log_exc("platform_detect import failed")
        os_info = None

    if install_mode == "prompt":
        # Show the user exactly what will run before they say yes
        if os_info is not None:
            print()
            print("Install commands that would run:")
            for name, imp, pac, pip_pkg in missing:
                cmd, source = install_command(os_info, pip_pkg)
                if cmd:
                    print(f"  [{source:>6}]  {cmd}")
                else:
                    print(f"  [{'??':>6}]  {name} — no install rule for this OS")
        if not _ask_yn("Install them now?", default=True):
            print("Aborting. Install manually with one of the commands above.")
            sys.exit(1)

    for name, imp, pac, pip_pkg in missing:
        print(f"\n→ installing {name}...")
        success = False
        if os_info is not None:
            from platform_detect import install_command
            cmd, source = install_command(os_info, pip_pkg)
            if cmd:
                try:
                    r = subprocess.run(cmd, shell=True, check=False)
                    success = (r.returncode == 0)
                except Exception as e:
                    print(f"  install command failed: {e}")
        # Last-resort: pip --user with break-system-packages
        if not success:
            success = _pip_install(pip_pkg)
        if not success:
            print(f"  ✗ failed to install {name}")
            print(f"    try manually: pip install --user {pip_pkg}")
            sys.exit(1)
        print(f"  ✓ installed {name}")

    # After installs, re-check. If a fresh `--user` install was just
    # done, the running process may not have the install path on
    # sys.path yet. Force re-resolution:
    import site
    importlib_imported = False
    try:
        import importlib
        importlib_imported = True
    except ImportError:
        pass
    if hasattr(site, "main"):
        site.main()
    if importlib_imported:
        importlib.invalidate_caches()  # type: ignore[name-defined]

    still_missing = [
        m[0] for m in missing if not _module_available(m[1])
    ]
    if still_missing:
        print()
        print(f"Installed but not yet importable: {', '.join(still_missing)}")
        print("Re-run this script and they'll be picked up.")
        sys.exit(0)


# =============================================================================
# MAIN — only imported after deps are confirmed available
# =============================================================================

def _format_size(n: int | None) -> str:
    if not n:
        return "0 B"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if n < 1024 or unit == "PiB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n} B"


# Module-level cache for the themed Rich Console. We DELIBERATELY don't
# cache it on the cfg dict — that gets TOML-serialised and Console isn't
# serialisable. Keyed by (theme, force_terminal, color_system) so changing
# theme via the menu rebuilds the console.
_CONSOLE_CACHE: dict[tuple, Any] = {}

# Lock guarding sys.stdout writes between the main thread and the
# background animator thread. Anyone writing to stdout in chunks larger
# than one print() call MUST hold this lock to avoid byte-level
# interleaving with the animator's frame writes.
import threading as _threading
_STDOUT_LOCK = _threading.Lock()


def _get_themed_console(cfg):
    """
    Build a Rich Console using the configured theme + force-terminal
    override. Returns None if Rich isn't available; callers fall back
    to plain print().
    """
    try:
        from rich.console import Console
    except ImportError:
        return None

    ui_cfg = cfg.get("ui", {})
    force = bool(ui_cfg.get("force_terminal", False))
    color_system = ui_cfg.get("color_system", "auto")
    theme_name = ui_cfg.get("theme", "cyber")

    key = (theme_name, force, color_system)
    cached = _CONSOLE_CACHE.get(key)
    if cached is not None:
        return cached

    kwargs = {}
    if force:
        kwargs["force_terminal"] = True
    if color_system != "auto":
        kwargs["color_system"] = color_system

    console = Console(**kwargs)
    _CONSOLE_CACHE[key] = console
    return console


def _invalidate_console_cache() -> None:
    """Call after a theme/colour-config change so the next render rebuilds."""
    _CONSOLE_CACHE.clear()


def _render_width(cfg, console) -> int:
    """
    Width to render the menu at. Honours `ui.fullwidth_background` —
    when true (default), uses the actual terminal width so the theme
    background extends edge-to-edge. When false, caps at 70 for a
    classic "menu in a box" feel.
    """
    if cfg.get("ui", {}).get("fullwidth_background", True):
        return console.width
    return min(console.width, 70)


def _config_is_complete(cfg: dict) -> tuple[bool, list[str]]:
    """
    Check whether the user has a usable config (sources + destination set).
    Returns (is_complete, list_of_missing_keys).
    """
    paths = cfg.get("paths", {})
    missing: list[str] = []
    sources = [s for s in paths.get("sources", []) if s]
    if not sources:
        missing.append("paths.sources")
    if not paths.get("destination_root", "").strip():
        missing.append("paths.destination_root")
    return (not missing, missing)


def _print_config_warning(cfg, db=None) -> None:
    """
    If the config is incomplete (no sources, or no destination_root),
    print a themed warning above the menu telling the user what to do.
    Called from main_menu just before _themed_menu so the warning is
    impossible to miss.
    """
    ok, missing = _config_is_complete(cfg)
    if ok:
        return

    console = _get_themed_console(cfg)
    if console is None:
        # plain fallback
        print()
        print("  ⚠  Your config is incomplete:")
        for k in missing:
            print(f"      missing: {k}")
        print("  Press 9 to run the first-time setup wizard.")
        return

    from ui import get_theme
    from rich.text import Text
    theme = get_theme(cfg.get("ui", {}).get("theme", "cyber"))
    width = _render_width(cfg, console)
    bg_style = f"on {theme.background}" if theme.background else ""

    def line(*segments: tuple[str, str]) -> Text:
        out = Text("  ", style=bg_style)
        for txt, style in segments:
            out.append(txt, style=_bg(theme, style))
        if theme.background:
            pad = max(0, width - out.cell_len)
            if pad:
                out.append(" " * pad, style=bg_style)
        return out

    blank = Text(" " * width, style=bg_style) if theme.background else Text()
    console.print(blank)
    console.print(line(
        ("!  ", f"bold {theme.accent}"),
        ("Your config is incomplete. Press ", theme.text),
        ("9", f"bold {theme.accent}"),
        (" to run first-time setup.", theme.text),
    ))
    for k in missing:
        console.print(line(
            ("    missing: ", theme.dim),
            (k, theme.text),
        ))



def _print_stats(db, cfg, *, console_override=None) -> None:
    """Themed library stats line.

    `console_override`: if provided, render to this console instead of
    the cached themed one. Used by the background animator."""
    s = db.stats()
    console = console_override if console_override is not None else _get_themed_console(cfg)

    if console is None:
        # plain fallback
        print()
        print(f"  Library: {s['total_files']:,} files, {_format_size(s['total_size_bytes'])}")
        print(f"  Distinct artists: {s['distinct_artists']:,}  "
              f"albums: {s['distinct_albums']:,}  labels: {s['distinct_labels']:,}")
        by_status = s.get("by_status") or {}
        if by_status:
            bits = "  ".join(f"{k or '(none)'}={v}" for k, v in by_status.items())
            print(f"  Status: {bits}")
        by_codec = s.get("by_codec") or {}
        if by_codec:
            top = sorted(by_codec.items(), key=lambda kv: -kv[1])[:6]
            print("  Codecs: " + ", ".join(f"{k or '?'}={v}" for k, v in top))
        print()
        return

    # themed
    from ui import get_theme
    from rich.text import Text
    theme = get_theme(cfg.get("ui", {}).get("theme", "cyber"))
    width = _render_width(cfg, console)

    def line(*segments: tuple[str, str]) -> Text:
        """Build a width-padded Text line with theme background painted."""
        bg_style = f"on {theme.background}" if theme.background else ""
        out = Text("  ", style=bg_style)
        for seg_text, seg_style in segments:
            out.append(seg_text, style=_bg(theme, seg_style))
        if theme.background:
            pad = max(0, width - out.cell_len)
            if pad:
                out.append(" " * pad, style=bg_style)
        return out

    blank = Text(" " * width, style=f"on {theme.background}") if theme.background else Text()

    try:
        from icons import icon as _ic
        i_files = _ic("folder")
        i_disk = _ic("hard_drive")
        i_artist = _ic("artist")
        i_album = _ic("album")
        i_label = _ic("tag_count")
    except Exception:
        i_files = i_disk = i_artist = i_album = i_label = "·"

    console.print(blank)
    console.print(line(
        ("Library:  ", f"bold {theme.label}"),
        (f"{i_files}  ", theme.accent),
        (f"{s['total_files']:,}", theme.accent),
        (" files,  ", theme.text),
        (f"{i_disk}  ", theme.accent),
        (_format_size(s["total_size_bytes"]), theme.text),
    ))
    console.print(line(
        ("Distinct: ", f"bold {theme.label}"),
        (f"{i_artist}  ", theme.accent),
        (f"{s['distinct_artists']:,}", theme.accent),
        (" artists   ", theme.text),
        (f"{i_album}  ", theme.accent),
        (f"{s['distinct_albums']:,}", theme.accent),
        (" albums   ", theme.text),
        (f"{i_label}  ", theme.accent),
        (f"{s['distinct_labels']:,}", theme.accent),
        (" labels", theme.text),
    ))
    by_status = s.get("by_status") or {}
    if by_status:
        # Map each status name to a glyph. Unknown statuses fall back to dot.
        try:
            from icons import icon as _ic
            status_icons = {
                "imported":   _ic("check"),
                "ok":         _ic("check"),
                "broken":     _ic("cross"),
                "duplicate":  _ic("file"),
                "skipped":    _ic("warning"),
                "orphan":     _ic("warning"),
                "pending":    _ic("hourglass_empty"),
                "unknown":    _ic("info"),
            }
        except Exception:
            status_icons = {}
        # Colour-tint each status counter by severity so the eye can
        # quickly find what's wrong without reading the label.
        status_styles = {
            "imported":   theme.success,
            "ok":         theme.success,
            "broken":     theme.error,
            "duplicate":  theme.duplicate,
            "skipped":    theme.warning,
            "orphan":     theme.warning,
            "pending":    theme.label,
            "unknown":    theme.dim,
        }
        segs: list[tuple[str, str]] = [("Status:   ", f"bold {theme.label}")]
        for i, (k, v) in enumerate(by_status.items()):
            if i:
                segs.append(("   ", theme.text))
            k_str = str(k or "(none)")
            ic = status_icons.get(k_str.lower(), "")
            style = status_styles.get(k_str.lower(), theme.dim)
            if ic:
                segs.append((f"{ic}  ", style))
            segs.append((k_str, style))
            segs.append(("=", theme.text))
            segs.append((str(v), style))
        console.print(line(*segs))
    by_codec = s.get("by_codec") or {}
    if by_codec:
        top = sorted(by_codec.items(), key=lambda kv: -kv[1])[:6]
        segs = [("Codecs:   ", f"bold {theme.label}")]
        for i, (k, v) in enumerate(top):
            if i:
                segs.append(("   ", theme.text))
            segs.append((str(k or "?"), theme.dim))
            segs.append(("=", theme.text))
            segs.append((str(v), theme.text))
        console.print(line(*segs))
    console.print(blank)


def _bg(theme, style: str) -> str:
    """
    Apply the theme's background to a foreground style, if set.
    "bold #ff0000" + bg "#fffeee"  ->  "bold #ff0000 on #fffeee"
    Idempotent: if `style` already contains " on ", we leave it alone.
    """
    if not theme.background:
        return style
    if " on " in style:
        return style
    return f"{style} on {theme.background}"


def _gradient_rule(theme, width: int, char: str = "═", offset: int = 0):
    """
    Build a Rich Text of `width` chars, each coloured from the theme's
    gradient palette with a per-call offset. The offset lets callers
    animate the rule by passing a frame counter.
    """
    from rich.text import Text
    txt = Text()
    palette = theme.gradient
    n = len(palette)
    for i in range(width):
        colour = palette[(i + offset) % n]
        txt.append(char, style=_bg(theme, colour))
    return txt


def _themed_header(cfg, *, frame: int = 0) -> None:
    """
    Header for the main menu. Renders:
      - top gradient rule
      - ASCII-art banner (bundled default, or any pyfiglet font)
      - footer URL line (from ui.logo_footer)
      - bottom gradient rule

    Each banner line gets its own gradient sweep so the whole block
    feels like one coloured unit. Frame offset marches the gradient
    sideways for animation.
    """
    console = _get_themed_console(cfg)
    if console is None:
        print("=" * 70)
        print("  music-organiser")
        print("=" * 70)
        return

    from ui import get_theme
    from rich.text import Text
    from logo import render_logo
    theme = get_theme(cfg.get("ui", {}).get("theme", "cyber"))
    ui_cfg = cfg.get("ui", {})

    width = _render_width(cfg, console)
    bg_style = f"on {theme.background}" if theme.background else ""

    # Generate the banner. Pass terminal width so render_logo can fall back
    # to a smaller font if the requested one is too wide.
    banner = render_logo(
        text=ui_cfg.get("logo_text", "music-organiser"),
        font=ui_cfg.get("logo_font", "default"),
        width=width - 2,
    )
    footer = ui_cfg.get("logo_footer", "")

    def paint_line(raw: str) -> Text:
        """Paint a single banner line with the rolling gradient + bg."""
        line = Text("", style=bg_style)
        palette = theme.gradient
        n = len(palette)
        for i, ch in enumerate(raw):
            if ch == " ":
                # Spaces just get the background; foreground colour wasted
                # on whitespace and risks washing out the bg colour.
                line.append(ch, style=bg_style)
            else:
                colour = palette[(i + frame) % n]
                line.append(ch, style=_bg(theme, f"bold {colour}"))
        # Pad to full width so the bg extends edge-to-edge.
        if theme.background:
            pad = max(0, width - line.cell_len)
            if pad:
                line.append(" " * pad, style=bg_style)
        return line

    console.print(_gradient_rule(theme, width, offset=frame))
    for raw_line in banner.splitlines():
        # Indent each banner line slightly so it doesn't kiss the left
        # margin. Cap length so we don't overflow on narrow terminals.
        clipped = raw_line[: max(0, width - 2)]
        console.print(paint_line(clipped))
    if footer:
        # Footer in dim/accent on its own row, centred-ish.
        footer_line = Text("  ", style=bg_style)
        footer_line.append(footer, style=_bg(theme, theme.dim))
        if theme.background:
            pad = max(0, width - footer_line.cell_len)
            if pad:
                footer_line.append(" " * pad, style=bg_style)
        console.print(footer_line)
    console.print(_gradient_rule(theme, width, offset=-frame))


def _themed_menu(cfg, *, console_override=None) -> None:
    """
    Render the main menu options with theme colours applied.

    Layout per row: "  <icon>  <key>  <Label>          <marquee_hint>"
      - Icon and key/label left-aligned
      - Marquee hint cycles between layman and technical text
      - Plain whitespace between (no dotted fill)
      - For options with a current-setting (theme/font/speed), the hint
        shows "current: X" instead of cycling

    The static path doesn't actually animate — it picks the current
    marquee phase based on time.time() and prints once. The animated
    main-menu loop is what gives it motion via background repaints.

    `console_override`: if provided, render to this console instead of
    the cached themed one. Used by the background animator to capture
    output into a StringIO buffer without monkey-patching globals.
    """
    console = console_override if console_override is not None else _get_themed_console(cfg)
    theme_name = cfg.get("ui", {}).get("theme", "cyber")
    logo_font = cfg.get("ui", {}).get("logo_font", "default")
    speed_tier = cfg.get("performance", {}).get("speed_level", "default")

    try:
        from menu_items import MENU_ITEMS, render_marquee
        full_items = [it for it in MENU_ITEMS if it.key]
    except Exception:
        # Defensive: never block startup. Build skeletal items.
        from types import SimpleNamespace
        full_items = [
            SimpleNamespace(key="1", icon="download", label="Import",
                            layman="bring new music in", technical=""),
            SimpleNamespace(key="?", icon="info", label="Guide",
                            layman="what each option does", technical=""),
            SimpleNamespace(key="q", icon="cross", label="Quit",
                            layman="exit", technical=""),
        ]
        render_marquee = None

    # Per-key current-state overlay. When set, the row's hint shows this
    # static string instead of cycling — so "current: cyber" replaces the
    # layman/technical marquee for the theme row, etc.
    try:
        from icons import speed_icon
        speed_glyph = speed_icon(speed_tier)
    except Exception:
        speed_glyph = ""
    current_state = {
        "t": f"current: {theme_name}",
        "l": f"current: {logo_font}",
        "p": (f"{speed_glyph}  current: {speed_tier}" if speed_glyph
              else f"current: {speed_tier}"),
    }

    # Detect icons availability
    try:
        from icons import icon, use_nerd_font
        nf = use_nerd_font()
    except Exception:
        icon = lambda name, fallback=None: fallback or "•"
        nf = False

    if console is None:
        # No Rich — plain output. Compute width from shutil.
        import shutil as _sh
        cols, _ = _sh.get_terminal_size(fallback=(100, 24))
        for idx, it in enumerate(full_items):
            ic = icon(it.icon)
            left = f"  {ic}  {it.key:<2} {it.label}"
            # Static text for current-state rows; layman for everything else
            hint_text = current_state.get(it.key)
            if hint_text is None and render_marquee is not None:
                hint_text, _mode = render_marquee(
                    it, row_index=idx, width=max(10, cols - len(left) - 6),
                )
            elif hint_text is None:
                hint_text = getattr(it, "layman", "")
            if hint_text:
                pad = max(2, cols - len(left) - len(hint_text) - 4)
                print(f"{left}{' ' * pad}{hint_text}")
            else:
                print(left)
        print()
        return

    from ui import get_theme
    from rich.text import Text
    theme = get_theme(theme_name)
    width = _render_width(cfg, console)
    bg_style = f"on {theme.background}" if theme.background else ""

    # Compute the longest left-side cell width so hints align consistently.
    # Cell width here is approximate — Nerd Font glyphs are 1-2 cells
    # depending on font/terminal. We assume 2 cells per glyph as a
    # conservative upper bound so the alignment doesn't drift.
    left_widths = []
    for it in full_items:
        ic = icon(it.icon)
        approx = 2 + (2 if nf else len(ic)) + 2 + len(it.key) + 2 + len(it.label)
        left_widths.append(approx)
    max_left = max(left_widths) if left_widths else 30
    hint_budget = max(10, width - max_left - 6)

    for idx, (it, left_w) in enumerate(zip(full_items, left_widths)):
        ic = icon(it.icon)
        t = Text("  ", style=bg_style)
        t.append(ic, style=_bg(theme, theme.accent))
        t.append("  ", style=bg_style)
        t.append(f"{it.key:<2}", style=_bg(theme, f"bold {theme.accent}"))
        t.append(" ", style=bg_style)
        t.append(it.label, style=_bg(theme, theme.text))

        # Pad-out so all hints align
        gap = max(2, max_left - left_w + 2)
        t.append(" " * gap, style=bg_style)

        # Build the hint — current-state overlay wins; otherwise marquee
        hint_text = current_state.get(it.key)
        hint_mode = "current"
        if hint_text is None and render_marquee is not None:
            hint_text, hint_mode = render_marquee(
                it, row_index=idx, width=hint_budget,
            )

        if hint_text:
            shown = hint_text
            if len(shown) > hint_budget:
                shown = shown[: hint_budget - 1].rstrip() + "…"
            # Style by mode: layman = dim, technical = dim italic,
            # current = dim bold. Keeps motion subtle, not distracting.
            if hint_mode == "technical":
                style = _bg(theme, f"dim italic {theme.dim}")
            elif hint_mode == "current":
                style = _bg(theme, f"bold {theme.dim}")
            else:
                style = _bg(theme, theme.dim)
            t.append(shown, style=style)

        # Pad to full width REGARDLESS of theme.background. Even when the
        # theme has no background colour, we still want trailing spaces
        # to clear any previous frame's marquee tail. Without this padding,
        # if frame N rendered a 180-char chunk for this row and frame N+1
        # renders a 90-char chunk, the last 90 chars of frame N's chunk
        # would remain on screen. Padding to full width overwrites them
        # with default-bg space cells.
        #
        # We add a fudge factor for Nerd Font glyphs, which Rich counts
        # as 1 cell but most terminals render as 2 cells wide. Without
        # it, padding under-reaches by ~1 cell per glyph in the row.
        pad = max(0, width - t.cell_len + (2 if nf else 0))
        if pad:
            t.append(" " * pad, style=bg_style if bg_style else "")
        console.print(t)

    if theme.background:
        console.print(Text(" " * width, style=bg_style))
    else:
        console.print()


def cmd_colour_test(cfg) -> None:
    """
    Diagnostic — print what Rich thinks about the terminal and a sample
    of every theme's palette. Helpful when colours look wrong.
    """
    _enter_subscreen(cfg)
    print()
    print(f"  $TERM       = {os.environ.get('TERM', '(unset)')}")
    print(f"  $COLORTERM  = {os.environ.get('COLORTERM', '(unset)')}")
    print(f"  $NO_COLOR   = {os.environ.get('NO_COLOR', '(unset)')}")
    print()

    try:
        from rich.console import Console
        from ui import THEMES
    except ImportError:
        print("  rich isn't installed — that's why everything is plain.")
        return

    # What does Rich auto-detect?
    auto = Console()
    print(f"  Rich auto-detect:")
    print(f"    color_system: {auto.color_system}")
    print(f"    is_terminal:  {auto.is_terminal}")
    print(f"    width:        {auto.width}")
    print()

    # Forced console — what we WOULD show if force_terminal=True
    forced = Console(force_terminal=True, color_system="truecolor")
    print(f"  Rich with force_terminal=True, color_system=truecolor:")
    forced.print("    [bold red]RED[/]  [bold green]GREEN[/]  "
                 "[bold yellow]YELLOW[/]  [bold blue]BLUE[/]  "
                 "[bold magenta]MAGENTA[/]  [bold cyan]CYAN[/]")
    forced.print("    [#ff00ff]#ff00ff[/]  [#00ffff]#00ffff[/]  "
                 "[#ffaa00]#ffaa00[/]  [#88ff44]#88ff44[/]   (truecolor swatches)")
    print()

    # Themes preview
    print("  All theme gradients:")
    for name, theme in THEMES.items():
        forced.print(f"    [bold {theme.accent}]{name:10s}[/]  ", end="")
        for c in theme.gradient:
            forced.print(f"[{c}]████[/]", end="")
        forced.print("")
    print()

    # If user is not currently force_terminal'ing, suggest it.
    ui_cfg = cfg.get("ui", {})
    if not ui_cfg.get("force_terminal", False):
        print("  If the above colours look right but the menu/import does NOT,")
        print("  edit your config and set under [ui]:")
        print("    force_terminal = true")
        print("    color_system = \"truecolor\"")
        print()


def _animate_menu_intro(cfg, db, *, fps: int = 18) -> None:
    """
    Brief animated render of the menu before falling through to input().

    Uses Rich's `Live` to repaint the header + stats + menu at `fps` for
    `ui.animate_menu_duration` seconds, then exits — leaving the final
    frame on the terminal as the static menu that input() reads against.

    Trade-off: we *can't* animate during input(). input() blocks and
    Rich's Live and the cursor would fight. So we animate on entry and
    settle. This gives the "wow it moves" effect when you switch menus
    without breaking keyboard input.

    If the config has `ui.animate_menu = false`, this is skipped and the
    menu renders statically once.
    """
    ui_cfg = cfg.get("ui", {})
    duration = float(ui_cfg.get("animate_menu_duration", 1.2))
    if not ui_cfg.get("animate_menu", True) or duration <= 0:
        # Static render
        _themed_header(cfg)
        _print_stats(db, cfg)
        _themed_menu(cfg)
        return

    try:
        from rich.console import Group
        from rich.live import Live
        from rich.text import Text
        from rich.console import Console
    except ImportError:
        # No rich -> plain
        _themed_header(cfg)
        _print_stats(db, cfg)
        _themed_menu(cfg)
        return

    console = _get_themed_console(cfg)
    if console is None:
        _themed_header(cfg)
        _print_stats(db, cfg)
        _themed_menu(cfg)
        return

    # Build a renderer that produces a Group of all menu pieces, varying
    # only by frame offset (so animation = the gradient marching sideways).
    from ui import get_theme
    theme = get_theme(ui_cfg.get("theme", "cyber"))

    def build(frame: int):
        width = _render_width(cfg, console)
        bg_style = f"on {theme.background}" if theme.background else ""

        def padded(t: Text) -> Text:
            """Pad a Text to full width with the bg style. Helps the bg
            colour stretch all the way across each row."""
            if theme.background:
                pad = max(0, width - t.cell_len)
                if pad:
                    t.append(" " * pad, style=bg_style)
            return t

        def paint_banner_line(raw: str) -> Text:
            """One row of the figlet banner, painted with rolling gradient."""
            line = Text("", style=bg_style)
            palette = theme.gradient
            n = len(palette)
            for i, ch in enumerate(raw):
                if ch == " ":
                    line.append(ch, style=bg_style)
                else:
                    colour = palette[(i + frame) % n]
                    line.append(ch, style=_bg(theme, f"bold {colour}"))
            return padded(line)

        # Header — ASCII banner instead of single-line title.
        from logo import render_logo
        banner = render_logo(
            text=ui_cfg.get("logo_text", "music-organiser"),
            font=ui_cfg.get("logo_font", "default"),
            width=width - 2,
        )
        banner_lines = [paint_banner_line(ln[: max(0, width - 2)])
                        for ln in banner.splitlines()]

        rule_top = _gradient_rule(theme, width, offset=frame)
        rule_bot = _gradient_rule(theme, width, offset=-frame)

        footer = ui_cfg.get("logo_footer", "")
        footer_line = None
        if footer:
            footer_line = Text("  ", style=bg_style)
            footer_line.append(footer, style=_bg(theme, theme.dim))
            padded(footer_line)

        # Stats — these don't animate; we just include them so the layout
        # remains stable through the animation.
        s = db.stats()
        blank = Text(" " * width, style=bg_style) if theme.background else Text()
        try:
            from icons import icon as _stat_icon
            i_files = _stat_icon("folder")
            i_disk = _stat_icon("hard_drive")
            i_artist = _stat_icon("artist")
            i_album = _stat_icon("album")
            i_label = _stat_icon("tag_count")
        except Exception:
            i_files = i_disk = i_artist = i_album = i_label = "·"

        stat_line1 = Text("  ", style=bg_style)
        stat_line1.append("Library:  ", style=_bg(theme, f"bold {theme.label}"))
        stat_line1.append(f"{i_files}  ", style=_bg(theme, theme.accent))
        stat_line1.append(f"{s['total_files']:,}", style=_bg(theme, theme.accent))
        stat_line1.append(" files,  ", style=_bg(theme, theme.text))
        stat_line1.append(f"{i_disk}  ", style=_bg(theme, theme.accent))
        stat_line1.append(_format_size(s["total_size_bytes"]), style=_bg(theme, theme.text))
        padded(stat_line1)

        stat_line2 = Text("  ", style=bg_style)
        stat_line2.append("Distinct: ", style=_bg(theme, f"bold {theme.label}"))
        stat_line2.append(f"{i_artist}  ", style=_bg(theme, theme.accent))
        stat_line2.append(f"{s['distinct_artists']:,}", style=_bg(theme, theme.accent))
        stat_line2.append(" artists   ", style=_bg(theme, theme.text))
        stat_line2.append(f"{i_album}  ", style=_bg(theme, theme.accent))
        stat_line2.append(f"{s['distinct_albums']:,}", style=_bg(theme, theme.accent))
        stat_line2.append(" albums   ", style=_bg(theme, theme.text))
        stat_line2.append(f"{i_label}  ", style=_bg(theme, theme.accent))
        stat_line2.append(f"{s['distinct_labels']:,}", style=_bg(theme, theme.accent))
        stat_line2.append(" labels", style=_bg(theme, theme.text))
        padded(stat_line2)

        # Menu items — pulled from menu_items for the canonical list
        theme_name = ui_cfg.get("theme", "cyber")
        logo_font = ui_cfg.get("logo_font", "default")
        speed_tier = cfg.get("performance", {}).get("speed_level", "default")
        try:
            from menu_items import MENU_ITEMS, render_marquee
            full_items = [it for it in MENU_ITEMS if it.key]
        except Exception:
            from types import SimpleNamespace
            full_items = [
                SimpleNamespace(key="1", icon="download", label="Import",
                                layman="bring new music in", technical=""),
                SimpleNamespace(key="?", icon="info", label="Guide",
                                layman="what each option does", technical=""),
                SimpleNamespace(key="q", icon="cross", label="Quit",
                                layman="exit", technical=""),
            ]
            render_marquee = None
        try:
            from icons import speed_icon as _spd
            speed_glyph = _spd(speed_tier)
        except Exception:
            speed_glyph = ""
        current_state = {
            "t": f"current: {theme_name}",
            "l": f"current: {logo_font}",
            "p": (f"{speed_glyph}  current: {speed_tier}" if speed_glyph
                  else f"current: {speed_tier}"),
        }
        try:
            from icons import icon
        except Exception:
            icon = lambda name, fallback=None: fallback or "•"

        # Calculate left-side widths
        left_widths = []
        for it in full_items:
            ic = icon(it.icon)
            approx = 2 + 2 + 2 + len(it.key) + 2 + len(it.label)
            left_widths.append(approx)
        max_left = max(left_widths) if left_widths else 30
        hint_budget = max(10, width - max_left - 6)

        menu_lines: list[Any] = []
        for idx, (it, left_w) in enumerate(zip(full_items, left_widths)):
            ic = icon(it.icon)
            line = Text("  ", style=bg_style)
            line.append(ic, style=_bg(theme, theme.accent))
            line.append("  ", style=bg_style)
            line.append(f"{it.key:<2}", style=_bg(theme, f"bold {theme.accent}"))
            line.append(" ", style=bg_style)
            line.append(it.label, style=_bg(theme, theme.text))
            gap = max(2, max_left - left_w + 2)
            line.append(" " * gap, style=bg_style)

            # Hint: current-state overlay wins; otherwise marquee
            hint_text = current_state.get(it.key)
            hint_mode = "current"
            if hint_text is None and render_marquee is not None:
                hint_text, hint_mode = render_marquee(
                    it, row_index=idx, width=hint_budget,
                )
            if hint_text:
                shown = (hint_text if len(hint_text) <= hint_budget
                         else hint_text[: hint_budget - 1].rstrip() + "…")
                if hint_mode == "technical":
                    style = _bg(theme, f"dim italic {theme.dim}")
                elif hint_mode == "current":
                    style = _bg(theme, f"bold {theme.dim}")
                else:
                    style = _bg(theme, theme.dim)
                line.append(shown, style=style)
            padded(line)
            menu_lines.append(line)
        menu_lines.append(blank)

        header_pieces: list[Any] = [rule_top, *banner_lines]
        if footer_line is not None:
            header_pieces.append(footer_line)
        header_pieces.append(rule_bot)

        return Group(*header_pieces, blank,
                     stat_line1, stat_line2, blank, *menu_lines)

    # Animate. KeyboardInterrupt during the loop is the user hitting
    # Ctrl-C while the intro plays — they want to skip or exit. We
    # collapse to the static final-frame render so the menu is visible,
    # and re-raise so main_menu can decide whether to quit.
    frame_count = max(1, int(duration * fps))
    try:
        with Live(build(0), console=console, refresh_per_second=fps,
                  screen=False, transient=False) as live:
            for f in range(1, frame_count + 1):
                time.sleep(1.0 / fps)
                live.update(build(f), refresh=True)
    except KeyboardInterrupt:
        # Re-raise so the main_menu loop can handle it as "user wants to
        # cancel this render but stay in the menu" — or, if it propagates
        # all the way up, main() now catches it and exits cleanly without
        # showing a stack trace.
        raise


def _background_header_animator(cfg, console, stop_event, db=None, fps: int = 14):
    """
    Continuously repaints just the header bands (top rule + banner +
    footer + bottom rule) on a background thread, while the main thread
    blocks on input(). Uses ANSI cursor save/restore so the user's input
    cursor isn't disturbed.

    The animation only touches the first N rows (the header). Stats and
    the menu below stay static. Stops when `stop_event` is set.

    On terminal resize, the entire menu (header + stats + menu items) is
    redrawn at the new width — not just the header — so the menu items
    below the animated region don't end up truncated. Pass `db` to enable
    this; without `db` we only redraw the header on resize.

    Trade-offs (be honest about these):
      - Some terminals flicker visibly. If yours does, set
        ui.animate_while_input = false.
      - If your input line wraps past terminal width, the cursor
        restore position is wrong. Keep the prompt short.
    """
    import sys
    import threading

    from ui import get_theme
    from rich.text import Text
    from rich.console import Console
    from logo import render_logo

    theme = get_theme(cfg.get("ui", {}).get("theme", "cyber"))
    ui_cfg = cfg.get("ui", {})

    # We need a Console we can render to in-memory so we can grab the
    # ANSI bytes and write them with our own cursor-save/restore wrapping.
    # The MAIN console has no idea this thread is writing.
    try:
        initial_width = console.width
    except Exception:
        initial_width = 100

    initial_size = None
    try:
        initial_size = os.get_terminal_size()
    except OSError:
        return  # No tty? Bail.

    bg_style = f"on {theme.background}" if theme.background else ""

    def render_header_frame(frame: int, width: int) -> str:
        """Render the header bands to an ANSI string."""
        # Build a Console that captures to a StringIO so we get bytes back.
        from io import StringIO
        buf = StringIO()
        cap_console = Console(
            file=buf,
            force_terminal=True,
            color_system="truecolor",
            width=width,
            legacy_windows=False,
        )

        banner = render_logo(
            text=ui_cfg.get("logo_text", "music-organiser"),
            font=ui_cfg.get("logo_font", "default"),
            width=width - 2,
        )

        def paint_line(raw: str) -> Text:
            line = Text("", style=bg_style)
            palette = theme.gradient
            n = len(palette)
            for i, ch in enumerate(raw):
                if ch == " ":
                    line.append(ch, style=bg_style)
                else:
                    colour = palette[(i + frame) % n]
                    line.append(ch, style=_bg(theme, f"bold {colour}"))
            if theme.background:
                pad = max(0, width - line.cell_len)
                if pad:
                    line.append(" " * pad, style=bg_style)
            return line

        cap_console.print(_gradient_rule(theme, width, offset=frame))
        for raw_line in banner.splitlines():
            clipped = raw_line[: max(0, width - 2)]
            cap_console.print(paint_line(clipped))
        footer = ui_cfg.get("logo_footer", "")
        if footer:
            footer_line = Text("  ", style=bg_style)
            footer_line.append(footer, style=_bg(theme, theme.dim))
            if theme.background:
                pad = max(0, width - footer_line.cell_len)
                if pad:
                    footer_line.append(" " * pad, style=bg_style)
            cap_console.print(footer_line)
        cap_console.print(_gradient_rule(theme, width, offset=-frame))
        return buf.getvalue()

    def render_menu_block_frame(width: int) -> str | None:
        """
        Render the stats + menu rows to an ANSI string. The menu's
        marquee reads time.time() per row, so calling this repeatedly
        produces the live-cycling hint text. Returns None if anything
        fails — caller skips the menu repaint in that case.

        Honest constraint: this writes MORE bytes per frame than the
        header alone, so we call it at a lower rate (see the rate-
        limit in the animator loop). The marquee only cycles every
        few seconds anyway, so 2fps menu refresh is plenty.

        Implementation: passes a capture console via console_override
        to _print_stats and _themed_menu. NO monkey-patching of globals
        means the main thread can call those same functions concurrently
        without interference — they just see the normal cached console.
        """
        try:
            from io import StringIO
            buf = StringIO()
            cap_console = Console(
                file=buf,
                force_terminal=True,
                color_system="truecolor",
                width=width,
                legacy_windows=False,
            )
            if db is not None:
                _print_stats(db, cfg, console_override=cap_console)
            _themed_menu(cfg, console_override=cap_console)
            return buf.getvalue()
        except Exception:
            return None

    frame = 0
    delay = 1.0 / fps
    last_size = initial_size
    current_width = initial_width

    def redraw_full_menu(width: int) -> None:
        """
        When the terminal resizes, the menu items and stats below the
        header are still at the old width — truncated or padded wrong.
        Redraw EVERYTHING using the static (non-animated) renderer.
        """
        if db is None:
            return
        try:
            _paint_screen_bg(cfg)
            _themed_header(cfg, frame=frame)
            _print_stats(db, cfg)
            _themed_menu(cfg)
            sys.stdout.flush()
        except Exception:
            pass

    # Menu block caching. We refresh the menu paint at 2fps (vs 14fps
    # for the header), since the menu's marquee only cycles every few
    # seconds. Caching avoids spending 80% of the animator's frame budget
    # re-rendering text that hasn't changed.
    menu_block_cache: str | None = None
    menu_block_last_refresh: float = 0.0
    MENU_REFRESH_INTERVAL = 0.5   # seconds — 2 fps

    try:
        while not stop_event.is_set():
            # Check terminal resize each frame.
            try:
                cur_size = os.get_terminal_size()
            except OSError:
                # No tty? Bail.
                return

            if cur_size != last_size:
                # Resize detected. Redraw the WHOLE menu at the new size.
                last_size = cur_size
                current_width = cur_size.columns
                # Drop the menu cache so it gets re-rendered at the new width.
                menu_block_cache = None
                # Acquire stdout lock so we don't interleave with main_menu.
                with _STDOUT_LOCK:
                    sys.stdout.write("\033[s\033[?25l")
                    redraw_full_menu(current_width)
                    sys.stdout.write("\033[?25h\033[u")
                    sys.stdout.flush()

            # Check for stop before doing the expensive render.
            if stop_event.is_set():
                break

            frame_str = render_header_frame(frame, current_width)

            # Refresh the menu cache periodically. The marquee in
            # menu_items reads time.time() per row, so re-rendering
            # at 2fps gives smooth marquee motion without burning
            # bytes at 14fps.
            now = time.time()
            if (menu_block_cache is None or
                now - menu_block_last_refresh >= MENU_REFRESH_INTERVAL):
                fresh = render_menu_block_frame(current_width)
                if fresh is not None:
                    menu_block_cache = fresh
                    menu_block_last_refresh = now

            # Check again right before writing — minimises the race window
            # where the main thread is about to start a sub-screen.
            if stop_event.is_set():
                break

            # Compute header row count so we know where the menu paint
            # should start (line N+1, where N is the header line count).
            header_lines = frame_str.count("\n")
            # Menu paint row in 1-based terminal coordinates
            menu_start_row = header_lines + 1

            # Atomically: save cursor → move home → write header →
            # (optionally) move to menu start row → write menu →
            # restore cursor.
            with _STDOUT_LOCK:
                if stop_event.is_set():
                    break
                out = (
                    "\033[s"        # save cursor
                    "\033[?25l"     # hide cursor
                    "\033[H"        # move to (1,1)
                    + frame_str
                )
                if menu_block_cache:
                    # The previous approach was to write the menu cache
                    # as one big string at \033[{menu_start_row};1H and
                    # rely on Rich's Text.append(" " * pad) padding to
                    # fully cover the terminal width. That underpadded
                    # by 1-2 cells when Nerd Font glyphs were rendered
                    # (Rich counts them as 1 cell, terminal renders 2),
                    # which left fragments of the PREVIOUS frame's
                    # marquee chunk visible at the right edge of each
                    # row. Looked like description text was bleeding
                    # between menu items.
                    #
                    # New approach: split the cache into lines, position
                    # the cursor on each line individually, write the
                    # line, then \033[K (Erase in Line) to clear from
                    # cursor to EOL using the current background. This
                    # guarantees no residue from previous frames no
                    # matter how badly Rich underestimates glyph widths.
                    lines = menu_block_cache.split("\n")
                    parts: list[str] = []
                    for i, line in enumerate(lines):
                        # Skip the empty trailing element after the final \n
                        if i == len(lines) - 1 and line == "":
                            continue
                        parts.append(f"\033[{menu_start_row + i};1H")
                        parts.append(line)
                        parts.append("\033[K")     # erase from cursor to EOL
                    out += "".join(parts)
                out += (
                    "\033[?25h"     # show cursor again
                    "\033[u"        # restore cursor
                )
                sys.stdout.write(out)
                sys.stdout.flush()

            frame += 1
            # Sleep in small chunks so we react to stop_event quickly.
            elapsed = 0.0
            tick = 0.01   # 10ms - faster reaction to stop signal
            while elapsed < delay and not stop_event.is_set():
                time.sleep(tick)
                elapsed += tick
    except Exception:
        # Never let a render bug kill the menu — just stop animating.
        return


# ANSI sequence to wipe the screen INCLUDING scrollback.
#   \033[H   move cursor to home (1,1)
#   \033[2J  clear visible screen
#   \033[3J  clear scrollback buffer (modern terminals: xterm, kitty,
#            alacritty, gnome-terminal, konsole, Windows Terminal, foot,
#            wezterm, iTerm2). On terminals that don't recognise it, the
#            sequence is silently ignored — no harm done.
# We do these manually instead of `os.system("clear")` because the
# external `clear` command often doesn't clear scrollback (depends on
# the terminfo entry), and we want consistent behaviour.
_CLEAR_SCREEN_ANSI = "\033[H\033[2J\033[3J"


def _paint_screen_bg(cfg) -> None:
    """
    If the active theme has a background colour and fullwidth_background
    is on, paint the ENTIRE terminal area with that bg, not just where
    Rich draws. This is what makes the 4chan theme actually look like
    a board page — the cream extends to every pixel.

    We use ANSI: set default bg attribute, clear screen+scrollback, move
    home. The set bg attribute persists for any subsequent print calls
    until something resets it, so the cream stays behind everything until
    we explicitly reset on quit.
    """
    from ui import get_theme
    ui_cfg = cfg.get("ui", {})

    def plain_clear():
        with _STDOUT_LOCK:
            sys.stdout.write(_CLEAR_SCREEN_ANSI)
            sys.stdout.flush()

    if not ui_cfg.get("fullwidth_background", True):
        plain_clear()
        return
    theme = get_theme(ui_cfg.get("theme", "cyber"))
    if not theme.background:
        plain_clear()
        return

    # Parse "#RRGGBB" to integers.
    bg = theme.background.lstrip("#")
    if len(bg) != 6:
        plain_clear()
        return
    try:
        r, g, b = int(bg[0:2], 16), int(bg[2:4], 16), int(bg[4:6], 16)
    except ValueError:
        plain_clear()
        return

    # 48;2 = set background to truecolor RGB. Then the standard
    # clear sequence (visible + scrollback).
    with _STDOUT_LOCK:
        sys.stdout.write(f"\033[48;2;{r};{g};{b}m{_CLEAR_SCREEN_ANSI}")
        sys.stdout.flush()


def _reset_screen_bg() -> None:
    """Reset terminal background to default + clear screen + scrollback.
    Called on quit so we don't leave the user's terminal painted cream
    or with stale content scrollable above."""
    with _STDOUT_LOCK:
        sys.stdout.write(f"\033[0m{_CLEAR_SCREEN_ANSI}")
        sys.stdout.flush()


def _enter_subscreen(cfg) -> None:
    """
    Called at the top of every cmd_* that does its own UI (theme picker,
    logo picker, audit screen, config viewer, etc).

    Wipes the menu underneath cleanly and re-applies the theme background
    so subsequent prints sit on top of the right colour. The active text
    foreground also gets nudged toward the theme's text colour so plain
    `print()` calls inherit roughly-themed output.

    On exit there's no cleanup needed — main_menu's loop calls
    `_paint_screen_bg(cfg)` at the top of every iteration, which clears
    and re-paints from scratch.
    """
    from ui import get_theme
    ui_cfg = cfg.get("ui", {})
    theme = get_theme(ui_cfg.get("theme", "cyber"))

    # Wipe to bg.
    _paint_screen_bg(cfg)

    # Set foreground for subsequent plain prints, so they're readable on
    # the theme's bg. Skip if the theme has no bg (terminal default fg
    # already works fine).
    if theme.background:
        # Pick a sensible fg. theme.text is what we use elsewhere, but for
        # readability on light backgrounds prefer something darker; on
        # dark backgrounds prefer something lighter. The theme designer
        # already chose text for contrast against bg.
        fg = _hex_rgb_seq(theme.text)
        if fg is not None:
            with _STDOUT_LOCK:
                sys.stdout.write(fg)
                sys.stdout.flush()


def _hex_rgb_seq(colour: str) -> str | None:
    """Turn a '#RRGGBB' hex string into an ANSI foreground SGR (38;2;R;G;B).
    Returns None for named colours or invalid hex — caller falls back to
    terminal default.
    """
    if not isinstance(colour, str) or not colour.startswith("#"):
        return None
    h = colour.lstrip("#")
    if len(h) != 6:
        return None
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return None
    return f"\033[38;2;{r};{g};{b}m"


def main_menu(cfg, db) -> None:
    import threading

    while True:
        _paint_screen_bg(cfg)
        _animate_menu_intro(cfg, db)
        _print_config_warning(cfg, db)

        # Continuous animation while waiting for input — only if enabled
        # and we have a real TTY (otherwise the cursor-save trick is moot).
        animate_thread = None
        stop_event = None
        ui_cfg = cfg.get("ui", {})
        animate_input = ui_cfg.get("animate_while_input", True)
        if animate_input and sys.stdout.isatty():
            console = _get_themed_console(cfg)
            if console is not None:
                stop_event = threading.Event()
                animate_thread = threading.Thread(
                    target=_background_header_animator,
                    args=(cfg, console, stop_event, db),
                    daemon=True,
                )
                animate_thread.start()

        try:
            choice = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            _log("main_menu got EOF/KeyboardInterrupt at input prompt")
            if stop_event is not None:
                stop_event.set()
            _reset_screen_bg()
            return
        finally:
            # Always stop the animator before doing anything else, so
            # subsequent prints don't get interleaved with header redraws.
            # Wait for the thread to ACTUALLY exit (not just signal it) —
            # otherwise its in-flight writes can clobber sub-screen output.
            # Thread exits within ~10ms of stop_event.set() due to the
            # inner sleep tick.
            if stop_event is not None:
                stop_event.set()
            if animate_thread is not None:
                animate_thread.join()

        _log("main_menu choice=%r", choice)

        # Dispatch table — every entry: (canonical_name, aliases_set, handler)
        # The handler is called with no args; closures capture cfg/db.
        # Accepting names ("import", "reindex", "audit") means you can type
        # any of: 1, i, import, imp, etc — all resolve to cmd_import.
        def do_vacuum() -> None:
            print("  vacuuming...")
            db.vacuum()
            print("  analysing...")
            db.analyse()
            print("  done.")

        def do_setup() -> None:
            from config import run_first_time_setup
            cfg.clear()
            cfg.update(run_first_time_setup())
            _invalidate_console_cache()

        def do_theme() -> None:
            cmd_theme(cfg)
            _invalidate_console_cache()

        def do_quit() -> None:
            raise _QuitMenu()

        dispatch = [
            # (number, name,        aliases,                    handler)
            ("1", "import",       {"imp", "i", "ingest", "in", "add"},
                                                                lambda: cmd_import(cfg, db)),
            ("2", "organise",     {"organize", "reorganise", "reorganize",
                                    "move", "sort", "restructure", "reorg"},
                                                                lambda: cmd_organise(cfg, db)),
            ("f", "fix-filenames", {"rename", "rename-files", "fix-names",
                                    "filenames", "tidy"},
                                                                lambda: cmd_fix_filenames(cfg, db)),
            ("x", "fix-broken",   {"rescue", "unbreak", "broken",
                                    "fix-broken-folder"},
                                                                lambda: cmd_fix_broken(cfg, db)),
            ("3", "rebuild",      {"reindex", "re-index", "idx", "index",
                                    "rebuild-db", "refresh", "rescan"},
                                                                lambda: cmd_reindex(cfg, db)),
            ("4", "check",        {"audit", "a", "check-db", "problems",
                                    "checks", "verify-db", "lint"},
                                                                lambda: cmd_audit(cfg, db)),
            ("5", "verify",       {"flac", "flacs", "verify-flacs",
                                    "fake-flac", "fakeflac", "rip-check",
                                    "spectral"},
                                                                lambda: cmd_verify_flacs(cfg, db)),
            ("6", "fix",          {"onetagger", "tagger", "ot", "fix-tags"},
                                                                lambda: cmd_onetagger(cfg, db)),
            ("7", "fetch",        {"musicbrainz", "mb", "tags", "metadata",
                                    "populate", "lookup"},
                                                                lambda: cmd_fetch_metadata(cfg, db)),
            ("8", "query",        {"sql", "select"},            lambda: cmd_query(db, cfg)),
            ("9", "vacuum",       {"analyze", "analyse", "compact",
                                    "optimize", "defrag"},      do_vacuum),
            ("0", "config",       {"show-config", "show", "settings",
                                    "cfg"},                     lambda: cmd_show_config(cfg)),
            ("e", "everything",   {"do-everything", "do", "all", "pipeline",
                                    "auto"},                    lambda: cmd_do_everything(cfg, db)),
            ("b", "browse",       {"library", "search", "find", "explorer",
                                    "browser"},                 lambda: cmd_browse(cfg, db)),
            ("r", "ripaudit",     {"rip-audit", "vamp", "lossy-audit",
                                    "audio-audit"},             lambda: cmd_rip_audit(cfg, db)),
            ("Q", "quarantine",   {"isolate", "fake-flac-move", "trash-fake"},
                                                                lambda: cmd_quarantine(cfg, db)),
            # Transcode is intentionally NOT in the main menu — it's an
            # opt-in advanced command. Reachable only by typing 'transcode'
            # or one of its aliases. The Q quarantine flow is the preferred
            # answer for fake-FLACs; transcode is for users who explicitly
            # accept the second-generation lossy tradeoff.
            ("",  "transcode",    {"reencode", "re-encode", "shrink",
                                    "transcode-suspects"},
                                                                lambda: cmd_transcode_suspects(cfg, db)),
            # Pony art is hidden too — just decoration. Type 'pony' to invoke.
            ("",  "pony",         {"mascot", "ponies", "ascii-art"},
                                                                lambda: cmd_pony(cfg, db)),
            # Deep audio analysis — actually inspect the bits to detect
            # fake-FLAC, fake-hi-res, etc. Hidden because it's slow
            # (~5-30s per file) and meant for spot-checking suspicious
            # rips rather than batch operation.
            ("",  "analyze",      {"analyse", "deep-analyse", "deep-analyze",
                                    "inspect", "audit-rip"},
                                                                lambda: cmd_analyze_rip(cfg, db)),
            ("?", "guide",        {"help", "h", "wat", "what", "?"},
                                                                lambda: cmd_guide(cfg, db)),
            ("s", "setup",        {"first-time", "wizard", "init",
                                    "reconfigure"},             do_setup),
            ("t", "theme",        {"colour", "color", "skin"}, do_theme),
            ("l", "logo",         {"font", "banner", "figlet"},  lambda: cmd_logo(cfg)),
            ("c", "colour",       {"color", "colour-test", "diag", "diagnose"},
                                                                lambda: cmd_colour_test(cfg)),
            ("p", "performance",  {"speed", "ludicrous", "fast", "perf",
                                    "tier"},                    lambda: cmd_speed(cfg)),
            ("d", "debug",        {"log", "debug-log", "tail"}, lambda: cmd_show_debug_log()),
            ("q", "quit",         {"exit", "bye", "x"},         do_quit),
        ]

        def dispatch_match(c: str):
            """Find the handler for the user's input. Returns the matched
            entry tuple or None.

            Supports a 'KEY?' suffix that means "show me the detail page
            for this key, not run the command" — handled by the caller."""
            cl = c.strip()
            if not cl:
                return None
            # Trailing '?' means detail view; we strip it here so dispatch
            # matching works, and the caller checks for the suffix.
            if cl.endswith("?") and len(cl) > 1:
                cl = cl[:-1].strip()
            cl_lower = cl.lower()
            for entry in dispatch:
                number, name, aliases, _handler = entry
                # Case-sensitive on single-char keys (so 'q' != 'Q'), but
                # case-insensitive on names/aliases (so 'IMPORT' = 'import')
                # Skip dispatch entries with no number key (those are
                # alias-only entries reachable by name only — e.g.
                # 'transcode' is intentionally hidden from the main
                # menu but available by typing the word.)
                if not number:
                    if cl_lower == name or cl_lower in {a.lower() for a in aliases}:
                        return entry
                    continue
                if len(cl) == 1 and cl == number:
                    return entry
                if cl_lower == name or cl_lower in {a.lower() for a in aliases}:
                    return entry
            # Fall back to prefix match against names.
            prefix = [e for e in dispatch if e[1].startswith(cl_lower)]
            if len(prefix) == 1:
                return prefix[0]
            return None

        entry = dispatch_match(choice)

        # If the user typed "KEY?" (e.g. "7?"), show the detail page for
        # that menu item instead of running its command.
        wants_detail = bool(choice.strip().endswith("?") and len(choice.strip()) > 1)
        if entry is not None and wants_detail:
            try:
                from menu_items import MENU_ITEMS
                # find the corresponding MenuItem by key
                key_lookup = entry[0]
                mi = next((it for it in MENU_ITEMS if it.key == key_lookup), None)
                if mi is not None:
                    _show_item_detail(mi)
                    continue
            except Exception:
                pass

        if entry is None:
            if choice:
                # Arrow keys leak through as escape sequences when readline
                # is missing or the terminal isn't in raw mode. Recognise
                # them and tell the user to use number/letter keys instead.
                # Sequences: \x1b[A/B/C/D for up/down/right/left, and
                # \x1b[1~ etc. for Home/End/PageUp/PageDown.
                if choice.startswith("\x1b["):
                    arrow_map = {
                        "\x1b[A": "up arrow", "\x1b[B": "down arrow",
                        "\x1b[C": "right arrow", "\x1b[D": "left arrow",
                        "\x1b[a": "up arrow", "\x1b[b": "down arrow",
                        "\x1b[c": "right arrow", "\x1b[d": "left arrow",
                        "\x1b[5~": "page up", "\x1b[6~": "page down",
                        "\x1b[H": "home", "\x1b[F": "end",
                    }
                    key = arrow_map.get(choice, "an escape sequence")
                    print(f"  (you pressed {key} — this menu uses number/letter keys.")
                    print(f"   try a number 1-9, a letter like 'b'/'?'/'q', or a name.)")
                else:
                    print(f"  no menu item matches '{choice}'. Try a number or name.")
                _log("main_menu unknown choice=%r", choice)
            # else: blank input, just redraw the menu
            try:
                input("\n[enter] to continue...")
            except (EOFError, KeyboardInterrupt):
                _reset_screen_bg()
                return
            continue

        number, name, _aliases, handler = entry
        _log("main_menu dispatch -> %s (%s)", name, number)
        try:
            handler()
        except _QuitMenu:
            _reset_screen_bg()
            return
        except KeyboardInterrupt:
            print("\n  (interrupted, back to menu)")
            _log("main_menu handler %s interrupted by KeyboardInterrupt", name)
        except Exception as e:
            # Don't let exceptions silently break the menu — log and show.
            _log_exc("main_menu handler %s raised %s", name, type(e).__name__)
            print()
            print(f"  ✗ Command '{name}' failed: {type(e).__name__}: {e}")
            log_p = get_debug_log_path()
            if log_p is not None:
                print(f"  See debug log: {log_p}")
        _log("main_menu handler %s returned", name)

        try:
            input("\n[enter] to continue...")
        except (EOFError, KeyboardInterrupt):
            _reset_screen_bg()
            return


def _make_progress_cb(progress, task_id):
    """Plain text fallback when no UI is active."""
    def cb(path: str, done: int, total: int) -> None:
        if total and (done % 50 == 0 or done == total):
            print(f"  {done}/{total}  {os.path.basename(path)}")
    return cb


def cmd_import(cfg, db) -> None:
    if not _check_writable(cfg):
        return
    _enter_subscreen(cfg)

    sources = cfg.get("paths", {}).get("sources", [])
    dest = cfg.get("paths", {}).get("destination_root", "").strip()
    if not sources:
        print()
        print("  No sources configured. Run option 9 (first-time setup) first,")
        print("  or edit your config.toml and add a [[paths]] sources entry.")
        return
    if not dest:
        print()
        print("  No destination_root configured. Run option 9 (first-time setup) first.")
        return

    # --- Resume detection -------------------------------------------------
    # If a previous import was interrupted (Ctrl+C, crash, power loss),
    # a checkpoint file is left on disk. Offer to resume.
    try:
        from checkpoint import (
            load_checkpoint, checkpoint_matches, describe_checkpoint,
            clear_checkpoint,
        )
    except ImportError as e:
        print(f"  ✗ Cannot load `checkpoint` module: {e}")
        print(f"     The music_organiser folder is missing a required file.")
        print(f"     Make sure all .py files are in the same folder as organiser.py")
        return
    existing_cp = load_checkpoint()
    if existing_cp and checkpoint_matches(existing_cp, sources, dest):
        print()
        print("  ┌─ A previous import was interrupted ────────────────────────")
        print(describe_checkpoint(existing_cp))
        print("  └────────────────────────────────────────────────────────────")
        print()
        print("  Resume from where it left off?")
        print("    Y — continue. Files already in the DB will be skipped")
        print("        automatically; the rest will be processed normally.")
        print("    N — start fresh. The checkpoint will be discarded, and")
        print("        every source file will be re-considered (already-")
        print("        imported files still get skipped via the DB cache).")
        if not _ask_yn("Resume?", default=True):
            clear_checkpoint()
            print("  ✓ checkpoint cleared, starting fresh")
        else:
            print("  ✓ resuming — the importer will skip files already in the DB")
    elif existing_cp:
        # Mismatched: leftover from a different operation. Discard.
        _log("cmd_import: discarding stale checkpoint (different sources/dest)")
        clear_checkpoint()

    print()
    print(f"Importing from {len(sources)} source(s):")
    for s in sources:
        exists = "✓" if Path(s).expanduser().exists() else "✗ NOT FOUND"
        print(f"  {s}  {exists}")
    print(f"Into: {dest}")
    print(f"Mode: {cfg['import']['mode']}")
    print()
    print("Database mode:")
    print("  Y (default) — update your library DB as files are organised.")
    print("                Required for dedup, audit, query, future re-organise.")
    print("  N           — organise files only, skip ALL DB writes. Uses a")
    print("                throwaway in-memory DB so nothing touches your")
    print("                library. Useful for one-off batches you don't want")
    print("                in your library catalogue.")
    use_real_db = _ask_yn("Update database during this run?", default=True)

    if not _ask_yn("Proceed?", default=True):
        return

    from importer import import_sources
    from ui import make_ui

    # Pick the DB to use. If the user said "no DB", swap in an ephemeral
    # in-memory SQLite — the importer's code path is identical, but no
    # writes hit disk. The real `db` is closed transiently and reopened
    # after the run.
    using_db: "Database"
    ephemeral_db_open = False
    if use_real_db:
        using_db = db
        _log("cmd_import: using real library DB at %s", db.path)
    else:
        from database import Database
        using_db = Database(":memory:", pragmas={})
        ephemeral_db_open = True
        _log("cmd_import: using EPHEMERAL in-memory DB (library DB untouched)")
        print()
        print("  → Using in-memory DB. Your library DB will not be modified.")

    ui_cfg = cfg.get("ui", {})
    ui = make_ui(
        theme=ui_cfg.get("theme", "cyber"),
        mode_label="Import" if use_real_db else "Import (no DB)",
        use_rich=ui_cfg.get("use_rich", True),
        refresh_per_second=ui_cfg.get("refresh_per_second", 20),
        force_terminal=ui_cfg.get("force_terminal", False),
        color_system=ui_cfg.get("color_system", "auto"),
    )

    try:
        with ui:
            stats = import_sources(sources, cfg=cfg, db=using_db, ui=ui)
    except KeyboardInterrupt:
        print()
        print("  ✗ interrupted by Ctrl+C")
        print("  Checkpoint preserved — re-run option 1 to resume.")
        _log("cmd_import: interrupted by KeyboardInterrupt; checkpoint preserved")
        return
    finally:
        # Discard the ephemeral DB if that's what we used.
        if ephemeral_db_open:
            try:
                using_db.close()
                _log("cmd_import: ephemeral DB closed and discarded")
            except Exception:
                pass

    print()
    print(stats.summary())
    if stats.errors:
        print(f"\nFirst 10 errors (of {len(stats.errors)}):")
        for e in stats.errors[:10]:
            print(f"  {e}")


def cmd_organise(cfg, db) -> None:
    """
    Re-organise files that are ALREADY in the destination tree, using
    the current DB metadata. No new imports happen.

    This is the natural follow-up to `Fetch tags (MusicBrainz)`:
    once your DB has clean tags, run this to make the on-disk
    folder layout match. Files with improved artist/album/year/label
    move to their canonical paths.

    Differences from option 1 (Import):
      - No source-folder scanning. Operates ONLY on files in the DB.
      - No copy mode — always moves. (Files are already at destination;
        we're just shifting them within the tree.)
      - Always uses the library DB (no in-memory ephemeral option —
        the DB IS the input here).
      - Offers a dry-run preview so you can see what would move
        before committing to a multi-gigabyte shuffle.

    Edge cases:
      - File missing on disk: skipped. (Run option 3 'Rebuild database'
        to clean up orphan DB rows.)
      - Two files would map to the same destination: name conflict
        resolved by suffixing " (2)", " (3)", etc.
      - File is already at its canonical path: counted as "in place",
        nothing happens.
      - Files marked status='broken': stay under Broken/, only their
        filename normalises.
    """
    if not _check_writable(cfg):
        return
    _enter_subscreen(cfg)

    n_files = db.count()
    if n_files == 0:
        print()
        print("  The library DB is empty — nothing to organise.")
        print("  Run option 1 (Import) to bring files in, or option 3")
        print("  (Rebuild database) if files are already in the destination.")
        return

    dest = cfg.get("paths", {}).get("destination_root", "").strip()
    if not dest:
        # Use cfg_ask so the user gets prompted with a default rather
        # than crashing.
        dest = cfg_ask(
            cfg, "paths.destination_root",
            default=str(Path.home() / "Organised"),
            prompt="Destination root",
            description=(
                "Re-organise needs to know where your library lives.\n"
                "This is the root of the 'High Quality / Label / Artist - Album / ...' tree."
            ),
        )
        if not dest:
            print("  no destination configured; aborting.")
            return

    print()
    print(f"  Library DB has {n_files:,} files.")
    print(f"  Destination root: {dest}")
    print()
    print("  This command will:")
    print("    • Read each file's current metadata from the DB.")
    print("    • Compute its canonical folder path.")
    print("    • Move any file that's not already at that path.")
    print("    • Clean up empty source folders after moves.")
    print()
    print("  It does NOT modify file metadata or fetch new info — it")
    print("  just shuffles files into their correct folders based on")
    print("  what the DB already says.")
    print()

    # Dry-run prompt — always offer this BEFORE the proceed prompt so
    # the user can see what would happen on a 12TB library before
    # committing to it.
    print("  Run modes:")
    print("    p — Preview only (dry-run, NOTHING moves)")
    print("    g — GO — actually move the files")
    print("    q — cancel")
    try:
        mode_raw = input("  > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if mode_raw == "q" or mode_raw in ("", "cancel"):
        return
    if mode_raw in ("p", "preview", "dry", "dry-run", "dryrun"):
        dry_run = True
    elif mode_raw in ("g", "go", "yes", "y", "real", "live"):
        dry_run = False
    else:
        print(f"  unknown mode '{mode_raw}'; aborting.")
        return

    if not dry_run:
        print()
        print("  ⚠  This will move files. The DB is the source of truth here —")
        print("     if some tags are wrong, files will move to wrong places.")
        print("     Consider running 'p' (preview) first to spot-check.")
        if not _ask_yn("Really proceed with live moves?", default=False):
            return

    # ----- Pass mode -----
    # Normal pass: skip rows whose organised_at is newer than their
    # last_seen (they were organised, and metadata hasn't changed
    # since). Much faster on a 207k-row library where most rows
    # haven't moved.
    # Fresh pass: re-evaluate every row regardless. Use when you
    # suspect drift between DB and disk (manual moves, restored
    # backup, edited the DB directly).
    print()
    print("  Pass mode:")
    print("    1. Normal pass — skip files we already organised and whose")
    print("       metadata hasn't changed since (default)")
    print("    2. Fresh pass — re-evaluate every file, no skipping")
    print()
    try:
        pass_choice = input("  Choose [1/2] (default 1): ").strip() or "1"
    except (EOFError, KeyboardInterrupt):
        return
    fresh_pass = (pass_choice == "2")
    if fresh_pass:
        print("  Fresh pass: every row will be processed.")
    else:
        print("  Normal pass: previously-organised unchanged rows will be skipped.")

    # ----- Online label lookup (v0.23.28) -----
    # When an album has missing/inconsistent label info, optionally
    # query MusicBrainz/Discogs for the missing data so the album lands
    # in the right label-folder rather than "Unknown Label/". Default
    # YES — most users want better organisation. Opt out for fully-
    # offline runs or when you're confident your tags are already
    # right.
    print()
    print("  Online label lookup:")
    print("    For albums missing or inconsistent label tags, query")
    print("    MusicBrainz/Discogs for the missing label. Only runs on")
    print("    albums that need it. Default YES.")
    print()
    try:
        lookup_ans = input("  Use online lookup? [Y/n]: ").strip().lower() or "y"
    except (EOFError, KeyboardInterrupt):
        return
    online_label_lookup = lookup_ans not in ("n", "no")
    if online_label_lookup:
        print("  Online lookup ENABLED. Provider rate limits apply.")
    else:
        print("  Online lookup DISABLED. Albums with missing labels go to "
              "'Unknown Label/' unless they're flagged as self-releases.")

    from importer import organise_in_place
    from ui import make_ui

    ui_cfg = cfg.get("ui", {})
    label = "Organise (preview)" if dry_run else "Organise"
    ui = make_ui(
        theme=ui_cfg.get("theme", "rainbowdash"),
        mode_label=label,
        use_rich=ui_cfg.get("use_rich", True),
        refresh_per_second=ui_cfg.get("refresh_per_second", 20),
        force_terminal=ui_cfg.get("force_terminal", False),
        color_system=ui_cfg.get("color_system", "auto"),
    )

    # Build the kwargs dict so we can adapt to whatever version of
    # organise_in_place is on disk. Some users hit a stale .pyc or an
    # unmerged install where the imported importer.py is older than
    # this organiser.py — passing an unknown kwarg would crash the
    # whole organise run with a TypeError. Inspect the signature and
    # only pass kwargs the function actually accepts.
    import inspect
    _oip_sig = inspect.signature(organise_in_place)
    _oip_kwargs: dict[str, Any] = {
        "cfg": cfg, "ui": ui, "dry_run": dry_run,
    }
    if "fresh_pass" in _oip_sig.parameters:
        _oip_kwargs["fresh_pass"] = fresh_pass
    if "online_label_lookup" in _oip_sig.parameters:
        _oip_kwargs["online_label_lookup"] = online_label_lookup
    elif online_label_lookup:
        # User asked for the new feature, but the importer.py on disk
        # is older and doesn't support it. Warn rather than silently
        # ignoring — they may not realise they're running a partial
        # upgrade.
        print()
        print("  ⚠  Online label lookup was requested but the installed")
        print("     importer.py is older and doesn't support it. The")
        print("     organise will run WITHOUT online lookup.")
        print("     Re-extract music_organiser.zip to get the full feature.")
        print()

    try:
        with ui:
            stats = organise_in_place(db, **_oip_kwargs)
    except KeyboardInterrupt:
        print()
        print("  ✗ interrupted. Files moved so far are committed in the DB.")
        return

    print()
    print(stats.summary())
    if dry_run:
        print()
        print("  ↑ This was a PREVIEW. To actually move files, run option 2")
        print("    again and choose 'g' for GO.")
    if stats.errors:
        print(f"\n  First 10 errors (of {len(stats.errors)}):")
        for e in stats.errors[:10]:
            print(f"    {e}")


def cmd_reindex(cfg, db) -> None:
    """
    Walk an already-organised tree and (re)populate the database from
    what's on disk. Useful after editing tags externally, or to rebuild
    the DB from scratch without re-importing.

    Defaults to the configured destination_root. If that's unset, lists
    your configured sources as candidates instead.
    """
    if not _check_writable(cfg):
        return
    _enter_subscreen(cfg)
    print()

    default_root = cfg.get("paths", {}).get("destination_root", "").strip()
    sources = [s for s in cfg.get("paths", {}).get("sources", []) if s]

    if default_root and Path(default_root).expanduser().exists():
        prompt = f"Tree to index [{default_root}]: "
    elif default_root:
        # configured but doesn't exist
        print(f"  Note: configured destination_root '{default_root}' doesn't exist on disk.")
        prompt = "Tree to index (path): "
        default_root = ""
    else:
        # not configured at all
        print("  No destination_root is configured.")
        if sources:
            print("  Your configured sources:")
            for s in sources:
                print(f"    {s}")
            print("  Enter one of those, or any other path you want to index.")
        else:
            print("  No sources configured either. Run option 9 (first-time setup) first,")
            print("  or type a path here to index just that folder.")
        prompt = "Tree to index (path): "

    try:
        entered = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return

    root = entered or default_root
    if not root:
        print("  no path given — back to menu.")
        return
    root_path = Path(root).expanduser()
    if not root_path.exists():
        print(f"  path doesn't exist: {root}")
        return
    if not root_path.is_dir():
        print(f"  path isn't a directory: {root}")
        return

    force = _ask_yn("Re-extract metadata for unchanged files?", default=False)

    from indexer import index_tree
    from ui import make_ui

    ui_cfg = cfg.get("ui", {})
    ui = make_ui(
        theme=ui_cfg.get("theme", "cyber"),
        mode_label="Index",
        use_rich=ui_cfg.get("use_rich", True),
        refresh_per_second=ui_cfg.get("refresh_per_second", 20),
        force_terminal=ui_cfg.get("force_terminal", False),
        color_system=ui_cfg.get("color_system", "auto"),
    )

    try:
        with ui:
            stats = index_tree(str(root_path), cfg=cfg, db=db, ui=ui, force=force)
    except KeyboardInterrupt:
        print("\ninterrupted.")
        return

    print()
    print(stats.summary())


# =============================================================================
# FIX FILENAMES — rename files to the pretty canonical format
# =============================================================================

# Kaomojis for the various states. Used in the UI ONLY, never in
# filenames. Soulseek users browsing your share don't want to type
# half-width katakana to find your tracks; the filenames stay ASCII-
# safe. The kaomojis live in the activity log / status display where
# they make the run feel less mechanical.
_KAOMOJI_START   = "(◠‿◠)"
_KAOMOJI_WORK    = "(￣▽￣)ノ"
_KAOMOJI_DONE    = "(*≧ω≦)"
_KAOMOJI_OOPS    = "(╥﹏╥)"
_KAOMOJI_THINK   = "(・_・ ヾ"
_KAOMOJI_SPARKLE = "☆.｡.:*"


def cmd_fix_filenames(cfg, db) -> None:
    """
    Rename files in place to a canonical pretty format. Useful when:
    Useful when:
      - You ran the importer before settling on a naming convention
      - Files in the Broken folder need rescuing now that tags exist
      - You want every file to follow the same
        ✰ [NN - Artist - Title - (freeform) - Album - Year - CODEC] Ripped By NAME.ext
        shape so other Soulseek users get consistent results browsing
        your share.

    Three SCOPES (which files to act on):
      1. Broken only      — files in the Broken folder (status='broken')
      2. ALL files        — every row in the DB
      3. Mismatched only  — files where the current basename doesn't
                             match what `build_pretty_filename` would
                             produce. Lets you skip the no-op renames.

    Two STRATEGIES (where the tags come from):
      A. Refetch from providers then rename — slower but produces
         correct names for files with bad/missing tags. Useful for the
         Broken folder where tags are usually the reason.
      B. Use DB only — fast. Skips any file lacking artist+album+title.
         Good for ALL/mismatched runs after a recent fetch.

    The first run prompts for a Soulseek username (saved to config) for
    The first run prompts for a Soulseek username (saved to config) for
    the optional `Ripped By NAME` attribution suffix. Blank = no suffix.

    DB integrity: `original_path` and `original_filename` are populated
    before the rename if they're NULL (set-once columns via COALESCE).
    After the rename, the `path` column gets the new path. Old DB rows
    keyed on the old path are deleted to avoid orphan rows.
    """
    _enter_subscreen(cfg)
    print()
    print(f"  {_KAOMOJI_SPARKLE} Fix filenames {_KAOMOJI_SPARKLE}")
    print()
    print("  Renames files to the canonical pretty format:")
    print("    ✰ [NN - Artist - Title - (freeform) - Album - Year - CODEC] Ripped By NAME.ext")
    print()
    print("  Example: ✰ [01 - Nujabes - 羽 Feather - Modal Soul - 2005 - FLAC] Ripped By anon.flac")
    print()

    # ----- Ensure soulseek_username is in config -----
    organise_cfg = cfg.setdefault("organise", {})
    rip_by = (organise_cfg.get("soulseek_username") or "").strip()
    if not rip_by:
        # First run — ask once, save. Blank is a valid answer (= no
        # attribution suffix appended).
        print(f"  {_KAOMOJI_THINK} Soulseek username for the 'Ripped By NAME' suffix?")
        print("  Press Enter to skip — filenames will have no attribution suffix.")
        try:
            entered = input("  Username: ").strip()
        except (EOFError, KeyboardInterrupt):
            entered = ""
        organise_cfg["soulseek_username"] = entered
        try:
            from config import save_config
            save_config(cfg)
            if entered:
                print(f"  Saved. Suffix will be: Ripped By {entered}")
            else:
                print("  Saved. No suffix will be added.")
        except Exception as e:
            print(f"  (couldn't save config: {e})")
        rip_by = entered
        print()

    # ----- Scope picker -----
    print("  Which files do you want to rename?")
    print("    1. Broken folder only  (files marked status='broken')")
    print("    2. ALL files in the library")
    print("    3. Mismatched only     (current name ≠ canonical format)")
    print()
    try:
        scope_choice = input("  Choose [1/2/3] (default 1): ").strip() or "1"
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if scope_choice not in {"1", "2", "3"}:
        print(f"  {_KAOMOJI_OOPS} Invalid choice — aborting.")
        return

    scope_label = {
        "1": "broken only",
        "2": "ALL files",
        "3": "mismatched only",
    }[scope_choice]

    # ----- Strategy picker -----
    print()
    print("  How should the tags be obtained?")
    print("    A. Refetch from MusicBrainz/Discogs first, THEN rename")
    print("       (slower, but fixes Broken files that have empty tags)")
    print("    B. Use the DB tags as-is (faster, skips files with no artist/title)")
    print()
    try:
        strat_choice = input("  Choose [A/B] (default B): ").strip().upper() or "B"
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if strat_choice not in {"A", "B"}:
        print(f"  {_KAOMOJI_OOPS} Invalid choice — aborting.")
        return

    print()
    print(f"  {_KAOMOJI_START} Starting:")
    print(f"    Scope    : {scope_label}")
    print(f"    Strategy : {'refetch then rename' if strat_choice == 'A' else 'DB only'}")
    print(f"    Suffix   : {('Ripped By ' + rip_by) if rip_by else '(none)'}")
    print()

    # ----- OPTIONAL: refetch pass (Strategy A) -----
    if strat_choice == "A":
        print(f"  {_KAOMOJI_WORK} Running tag fetch first…")
        print("  (this can take a while; the fetch checkpoint will resume if interrupted)")
        try:
            cmd_fetch_metadata(cfg, db)
        except KeyboardInterrupt:
            print()
            print(f"  {_KAOMOJI_OOPS} Fetch interrupted — continuing to rename pass anyway.")
        print()

    # ----- Build the work list -----
    paths_cfg = cfg.get("paths") or {}
    dest_root = Path(paths_cfg.get("destination_root", "")).expanduser()
    broken_subdir = paths_cfg.get("broken_folder", "Broken")
    broken_root = dest_root / broken_subdir

    organise_cfg = cfg.get("organise") or {}
    illegal = organise_cfg.get("illegal_path_chars", '<>:"/\\|?*\x00')
    max_len = organise_cfg.get("max_component_length", 200)

    try:
        from organiser_core import build_pretty_filename
    except Exception as e:
        print(f"  {_KAOMOJI_OOPS} couldn't import filename builder: {e}")
        return

    # Pull every row out of the DB. Filter according to scope.
    candidates: list[tuple[dict, Path, str]] = []  # (row_dict, current_path, new_filename)
    n_scanned = 0
    n_skipped_missing_tags = 0
    n_skipped_already_named = 0
    n_skipped_file_missing = 0
    n_skipped_not_in_scope = 0

    print(f"  {_KAOMOJI_THINK} Scanning DB for files to rename…")
    for row in db.iter_all():
        n_scanned += 1
        current_path_str = row.get("path") or ""
        if not current_path_str:
            continue
        current_path = Path(current_path_str)

        # Scope filter
        if scope_choice == "1":
            # Broken only: file's parent is under broken_root, OR
            # status=='broken' in the DB.
            in_broken = False
            try:
                current_path.relative_to(broken_root)
                in_broken = True
            except (ValueError, OSError):
                pass
            if not in_broken and (row.get("status") or "") != "broken":
                n_skipped_not_in_scope += 1
                continue

        # Build the target filename. Returns None if artist+title absent.
        new_filename = build_pretty_filename(
            row,
            illegal_chars=illegal,
            max_length=max_len,
            rip_by=rip_by,
        )
        if new_filename is None:
            n_skipped_missing_tags += 1
            continue

        # Scope 3 (mismatched only): skip files whose current basename
        # already matches the canonical form. No-op renames cost
        # nothing but they clutter the preview and the activity log.
        if scope_choice == "3" and current_path.name == new_filename:
            n_skipped_already_named += 1
            continue

        # Verify the source file exists. A DB row pointing at a missing
        # file means somebody moved/deleted it outside the script. We
        # can't rename what isn't there.
        if not current_path.exists():
            n_skipped_file_missing += 1
            continue

        candidates.append((dict(row), current_path, new_filename))

    print(f"    {n_scanned:,} rows scanned")
    print(f"    {len(candidates):,} files would be renamed")
    if n_skipped_not_in_scope:
        print(f"    {n_skipped_not_in_scope:,} skipped (outside the chosen scope)")
    if n_skipped_missing_tags:
        print(f"    {n_skipped_missing_tags:,} skipped (no artist/title in DB — can't build name)")
    if n_skipped_already_named:
        print(f"    {n_skipped_already_named:,} skipped (already correctly named)")
    if n_skipped_file_missing:
        print(f"    {n_skipped_file_missing:,} skipped (file missing on disk)")
    print()

    if not candidates:
        print(f"  {_KAOMOJI_DONE} Nothing to rename.")
        return

    # ----- Dry-run preview -----
    sample = candidates[:8]
    print(f"  {_KAOMOJI_SPARKLE} Preview (first {len(sample)} of {len(candidates):,}):")
    for _, src, new_name in sample:
        # Truncate long names for the preview display
        old_disp = src.name if len(src.name) <= 64 else src.name[:61] + "…"
        new_disp = new_name if len(new_name) <= 64 else new_name[:61] + "…"
        print(f"    {old_disp}")
        print(f"      → {new_disp}")
    print()

    if not _ask_yn(f"  {_KAOMOJI_WORK} Proceed with renaming?", default=False):
        print(f"  {_KAOMOJI_THINK} Cancelled. No files were touched.")
        return

    # ----- Execute the renames -----
    n_done = 0
    n_failed = 0
    n_conflict = 0
    print()
    print(f"  {_KAOMOJI_WORK} Renaming…")

    for row, src, new_name in candidates:
        target = src.parent / new_name

        # Don't trample an existing different file.
        if target.exists() and target != src:
            n_conflict += 1
            # Add a "(2)", "(3)" suffix until unique.
            stem, dot, ext = new_name.rpartition(".")
            i = 2
            while True:
                candidate = src.parent / f"{stem} ({i}).{ext}" if dot else src.parent / f"{new_name} ({i})"
                if not candidate.exists():
                    target = candidate
                    break
                i += 1
                if i > 99:
                    # Pathological — bail on this one rather than loop
                    n_failed += 1
                    target = None
                    break
            if target is None:
                continue

        try:
            src.rename(target)
        except OSError as e:
            n_failed += 1
            if n_failed <= 5:
                print(f"    {_KAOMOJI_OOPS} couldn't rename {src.name}: {e}")
            continue

        # Update the DB: insert a new row at the new path (carrying all
        # the old row's fields PLUS original_path/original_filename if
        # they were NULL), then delete the old row. We can't UPDATE the
        # PRIMARY KEY in place via the existing upsert pathway, so
        # delete+insert is the cleanest way.
        try:
            new_record = dict(row)
            new_record["path"] = str(target)
            # Set-once columns: only fill if they're currently blank.
            # COALESCE in upsert_file will preserve any existing value.
            if not new_record.get("original_path"):
                new_record["original_path"] = str(src)
            if not new_record.get("original_filename"):
                new_record["original_filename"] = src.name
            db.upsert_file(new_record)
            db.delete_by_path(str(src))
        except Exception as e:
            # The file moved successfully on disk but the DB update
            # failed. Log loudly — the DB is now out of sync with disk
            # for this one row. The user can re-run "Rebuild database"
            # to recover.
            n_failed += 1
            if n_failed <= 5:
                print(f"    {_KAOMOJI_OOPS} renamed on disk but DB update failed for "
                      f"{target.name}: {e}")
            continue

        n_done += 1
        if n_done % 100 == 0:
            print(f"    {_KAOMOJI_WORK} {n_done:,} / {len(candidates):,} renamed…")

    # ----- Summary -----
    print()
    print(f"  {_KAOMOJI_DONE} Done.")
    print(f"    Renamed   : {n_done:,}")
    if n_conflict:
        print(f"    Conflicts : {n_conflict:,} (suffixed with (2), (3), …)")
    if n_failed:
        print(f"    Failed    : {n_failed:,}  {_KAOMOJI_OOPS}")
    # Commit any pending DB writes — the upsert + delete_by_path pair
    # auto-commits in delete_by_path, but be explicit just in case.
    try:
        db.commit()
    except Exception:
        pass


# =============================================================================
# FIX BROKEN — full rescue pipeline scoped to the Broken folder
# =============================================================================

def cmd_fix_broken(cfg, db) -> None:
    """
    Targeted rescue for files in the Broken folder.

    The Broken folder collects files marked status='broken' — typically
    because they were missing artist, album, or title when imported.
    They're not lost (audio data is intact), they're just sequestered
    until tags can be recovered.

    This command runs the four-phase rescue pipeline against ONLY
    those files. Non-broken files are not touched.

      Phase 1 — REFETCH
          Run cmd_fetch_metadata against the broken set. Hits providers
          for the artist/album/title we couldn't determine at import.
          Successful matches write tags back to the audio file AND to
          the DB. Recovery chain (filename/folder parsing → sibling
          inference) gives the providers something to query with.

      Phase 2 — RE-EVALUATE
          Run audit.mark_broken_metadata. Files that now have all
          three required tags get flipped back to status='ok'. Files
          still missing tags keep status='broken' — there's only so
          much we can do if MusicBrainz and Discogs don't have it.

      Phase 3 — REORGANISE
          Run cmd_organise. Files that are no longer broken get moved
          out of Broken/ into their proper destinations (High Quality
          for lossless, Shit Quality for lossy). Files still broken
          stay where they are.

      Phase 4 — FIX FILENAMES  (optional, asked)
          Run the pretty-filename rename pass on rescued files so
          their names match the canonical format. Off by default —
          you might prefer to do this in a separate Fix-filenames
          run with your own scope choice.

    Each phase can be skipped if you've recently done it manually.
    Cancellation (Ctrl-C) at any phase saves the work done so far —
    the fetch checkpoint resumes, the audit changes are committed,
    organise moves are atomic per-file.
    """
    _enter_subscreen(cfg)
    print()
    print(f"  {_KAOMOJI_SPARKLE} Fix broken — rescue pipeline {_KAOMOJI_SPARKLE}")
    print()

    # ----- Discover the broken set --------------------------------
    # Two sources of "broken":
    #   1. DB rows with status='broken'
    #   2. Files physically under the Broken/ folder (in case the
    #      status was somehow cleared but the files weren't moved)
    # We union them so neither source can hide a broken file from
    # the rescue.
    paths_cfg = cfg.get("paths") or {}
    dest_root = Path(paths_cfg.get("destination_root", "")).expanduser()
    broken_subdir = paths_cfg.get("broken_folder", "Broken")
    broken_root = dest_root / broken_subdir

    broken_by_db = 0
    broken_by_path = 0
    seen_paths: set[str] = set()
    try:
        for row in db.iter_all():
            p = row.get("path") or ""
            if not p:
                continue
            is_db_broken = (row.get("status") or "").strip().lower() == "broken"
            in_broken_path = False
            try:
                Path(p).relative_to(broken_root)
                in_broken_path = True
            except (ValueError, OSError):
                pass
            if is_db_broken or in_broken_path:
                if is_db_broken:
                    broken_by_db += 1
                if in_broken_path and not is_db_broken:
                    broken_by_path += 1
                seen_paths.add(p)
    except Exception as e:
        print(f"  {_KAOMOJI_OOPS} couldn't scan DB: {e}")
        return

    n_total = len(seen_paths)
    print(f"  {_KAOMOJI_THINK} Scope: {n_total:,} files currently broken")
    print(f"    {broken_by_db:,} marked status='broken' in the DB")
    if broken_by_path:
        print(f"    {broken_by_path:,} live under {broken_root} but not flagged in DB")
    print()

    if n_total == 0:
        print(f"  {_KAOMOJI_DONE} Nothing to do — Broken folder is empty.")
        print()
        print("  (If you suspect files SHOULD be broken but aren't flagged,")
        print("   try option 4 → 'b' for a retroactive metadata audit.)")
        return

    # ----- Phase pickers ------------------------------------------
    # Each phase is independently opt-in. The pipeline is structured
    # so each phase produces useful output even if subsequent ones
    # are skipped (e.g. you might want to refetch tags but NOT
    # auto-rename in the same run).
    print(f"  {_KAOMOJI_START} The rescue pipeline has 4 phases:")
    print()
    print("    1. REFETCH       hit providers for the broken files'")
    print("                     missing tags")
    print("    2. RE-EVALUATE   flip status back to 'ok' for files")
    print("                     that now have all required tags")
    print("    3. REORGANISE    move un-broken files to their proper")
    print("                     destination (High Quality / Shit Quality)")
    print("    4. FIX FILENAMES rename rescued files to the canonical")
    print("                     ✰ [...] Ripped By NAME format")
    print()
    print("  Skip phases you've recently done manually.")
    print()

    do_refetch    = _ask_yn("  Phase 1: REFETCH from providers?",     default=True)
    do_reeval     = _ask_yn("  Phase 2: RE-EVALUATE broken status?",  default=True)
    do_reorganise = _ask_yn("  Phase 3: REORGANISE rescued files?",   default=True)
    do_rename     = _ask_yn("  Phase 4: FIX FILENAMES?",              default=False)
    print()

    if not any([do_refetch, do_reeval, do_reorganise, do_rename]):
        print(f"  {_KAOMOJI_THINK} No phases selected. Nothing to do.")
        return

    # ----- Phase 1: REFETCH ---------------------------------------
    if do_refetch:
        print(f"  {_KAOMOJI_WORK} Phase 1/4: REFETCH")
        print()
        print("  Running the standard fetch flow. This walks every album")
        print("  in the DB (not just broken ones) — the existing fetch")
        print("  checkpoint will resume if interrupted. The 'only_missing'")
        print("  flag means properly-tagged files are untouched.")
        print()
        try:
            cmd_fetch_metadata(cfg, db)
        except KeyboardInterrupt:
            print()
            print(f"  {_KAOMOJI_OOPS} Refetch interrupted. The fetch checkpoint")
            print("  saved partial progress; subsequent phases will use whatever")
            print("  got fetched before the interrupt.")
            print()
        # After fetch returns we re-enter the subscreen (cmd_fetch_metadata
        # may have rendered its own UI).
        _enter_subscreen(cfg)
        print()

    # ----- Phase 2: RE-EVALUATE -----------------------------------
    if do_reeval:
        print(f"  {_KAOMOJI_WORK} Phase 2/4: RE-EVALUATE broken status")
        try:
            from audit import mark_broken_metadata
        except ImportError as e:
            print(f"  {_KAOMOJI_OOPS} couldn't load mark_broken_metadata: {e}")
            return
        # Dry-run first so the user sees what would change.
        n_broken_dry, n_unbroken_dry = mark_broken_metadata(db, dry_run=True)
        print(f"    Would mark broken : {n_broken_dry:,}")
        print(f"    Would un-break    : {n_unbroken_dry:,}")
        if n_broken_dry > 0 or n_unbroken_dry > 0:
            print()
            if _ask_yn("  Apply these status changes?", default=True):
                n_broken, n_unbroken = mark_broken_metadata(db, dry_run=False)
                print(f"  {_KAOMOJI_DONE} {n_broken:,} marked broken, {n_unbroken:,} un-broken")
            else:
                print(f"  {_KAOMOJI_THINK} Skipped status updates.")
        else:
            print(f"  {_KAOMOJI_DONE} No status changes needed.")
        print()

    # ----- Phase 3: REORGANISE ------------------------------------
    if do_reorganise:
        print(f"  {_KAOMOJI_WORK} Phase 3/4: REORGANISE")
        print()
        print("  Running organise_in_place. Files that were broken and now")
        print("  aren't will move out of Broken/ to their proper destinations.")
        print("  Files still broken stay put. The same call also fixes any")
        print("  MP3-in-High-Quality mistakes from older builds.")
        print()
        try:
            from importer import organise_in_place
            from ui import make_ui
            ui = make_ui(cfg, mode_label="Organise (rescue)")
            with ui:
                organise_in_place(db, cfg=cfg, ui=ui, dry_run=False)
        except KeyboardInterrupt:
            print()
            print(f"  {_KAOMOJI_OOPS} Organise interrupted. Files moved so far stay moved;")
            print("  the rest still need a future organise pass.")
        except Exception as e:
            print(f"  {_KAOMOJI_OOPS} Organise failed: {e}")
        _enter_subscreen(cfg)
        print()

    # ----- Phase 4: FIX FILENAMES (optional) ----------------------
    if do_rename:
        print(f"  {_KAOMOJI_WORK} Phase 4/4: FIX FILENAMES")
        print()
        print("  Delegating to cmd_fix_filenames. You'll get the usual scope")
        print("  picker — pick 'broken only' or 'mismatched only' for the")
        print("  narrowest-effect run. Strategy B (DB only) is fastest here")
        print("  since phase 1 already did the fetching.")
        print()
        try:
            cmd_fix_filenames(cfg, db)
        except KeyboardInterrupt:
            print()
            print(f"  {_KAOMOJI_OOPS} Rename interrupted.")
        _enter_subscreen(cfg)

    print()
    print(f"  {_KAOMOJI_DONE} Rescue pipeline complete.")
    print()
    print("  If files are STILL in Broken/ after this, the providers simply")
    print("  don't have records for them. Options:")
    print("    - Try a different provider order (Discogs covers underground")
    print("      material MB lacks)")
    print("    - Edit tags manually in Picard / mp3tag / option 8 (SQL)")
    print("    - Use option f with strategy A to combine refetch + rename")
    print()



    """Run fake-FLAC detection on every lossless file in the DB."""
    _enter_subscreen(cfg)
    print()
    try:
        from fake_flac import (
            verify_lossless_in_db, dependencies_available,
            missing_dependencies,
        )
    except ImportError as e:
        print(f"  fake_flac module failed to import: {e}")
        return

    if not dependencies_available():
        miss = missing_dependencies()
        print(f"  Missing required packages: {', '.join(miss)}")
        print()
        print("  Install with one of:")
        print(f"    sudo pacman -S {' '.join(f'python-{m}' for m in miss)}")
        print(f"    pip install --user {' '.join(miss)}")
        if _ask_yn("\nAttempt to auto-install now via the bootstrapper?", default=True):
            for m in miss:
                ok = False
                try:
                    ok = _pacman_install(f"python-{m}")
                except Exception:
                    pass
                if not ok:
                    _pip_install(m)
            print()
            print("  Restart the program to pick up the new packages.")
        return

    # How many lossless rows are there?
    count = db.conn.execute(
        "SELECT COUNT(*) FROM files WHERE lossless = 1 AND status != 'broken'"
    ).fetchone()[0]
    already = db.conn.execute(
        "SELECT COUNT(*) FROM files WHERE transcode_checked = 1"
    ).fetchone()[0]
    suspect = db.conn.execute(
        "SELECT COUNT(*) FROM files WHERE transcode_suspected = 1"
    ).fetchone()[0]

    print(f"  Lossless files in DB:  {count:,}")
    print(f"  Already checked:       {already:,}")
    print(f"  Previously suspect:    {suspect:,}")
    print()
    force = _ask_yn("Re-check files that have already been analysed?", default=False)
    print()
    print("  This reads ~8 seconds from the middle of each file and runs an FFT.")
    print("  Expect ~50-100 ms per file; a 100k-file library is roughly 1-2 hours.")
    print()
    if not _ask_yn("Start?", default=True):
        return

    from ui import make_ui
    ui_cfg = cfg.get("ui", {})
    ui = make_ui(
        theme=ui_cfg.get("theme", "cyber"),
        mode_label="Verify FLACs",
        use_rich=ui_cfg.get("use_rich", True),
        refresh_per_second=ui_cfg.get("refresh_per_second", 20),
        force_terminal=ui_cfg.get("force_terminal", False),
        color_system=ui_cfg.get("color_system", "auto"),
    )

    try:
        with ui:
            stats = verify_lossless_in_db(db, ui=ui, force=force)
    except KeyboardInterrupt:
        print("\ninterrupted.")
        return
    except RuntimeError as e:
        print(f"\n  error: {e}")
        return

    print()
    print(stats.summary())
    if stats.files_suspect > 0:
        print()
        print(f"  Found {stats.files_suspect} files suspected of being transcoded.")
        print()
        print("  Next steps:")
        print("    • menu option 4 → 'suspected_transcode' to export the list")
        print("    • run Vamp audio-net confirm on these suspects now?")
        print()
        if _ask_yn("Run Vamp confirm on the suspects?", default=False):
            # Reuses cmd_rip_audit — it has its own dep checks and prompts.
            try:
                cmd_rip_audit(cfg, db)
            except Exception as e:
                print(f"  Vamp audit failed: {e}")
        print()
        print("  Confirmed-transcode files can be moved to a Quarantine folder")
        print("  via menu option 'q' (or 'quarantine'). They stay in the DB so")
        print("  you can review them, but won't pollute your lossless library.")


# =============================================================================
# AUDIT — missing art, bad metadata, dump reports
# =============================================================================

def cmd_audit(cfg, db) -> None:
    """Run metadata audits, show counts, offer to export reports."""
    try:
        from audit import AUDITS, audit_all, dump_full_report, dump_report
    except ImportError as e:
        print(f"  ✗ Cannot load `audit` module: {e}")
        print(f"     The music_organiser folder is missing audit.py")
        return

    _enter_subscreen(cfg)

    print()
    print("  Running all audits...")
    report = audit_all(db)
    print()
    print(f"  Total issues: {report.total_issues():,}")
    print()

    # Show counts side-by-side
    by_key = report.issues_by_audit
    for i, (key, (label, _)) in enumerate(AUDITS.items(), 1):
        n = len(by_key.get(key, []))
        marker = " " if n == 0 else "!"
        print(f"   {marker} {i:2d}. {label:50s} {n:>6,}")
    print()
    print("  Options:")
    print("    a. Export ALL findings (one file per audit) to a directory")
    print("    1-13. Export ONE specific audit to a file")
    print("    b. Mark broken: flip status='broken' on rows missing artist/album/title")
    print("       (and un-break rows that now have all three after a fetch)")
    print("    r. Reconcile album fields: fill MISSING label/year/genre on tracks")
    print("       whose album-mates have them. Conflicts auto-resolve by majority.")
    print("    l. Unify record labels: force every track in an album to share")
    print("       ONE label (majority vote). Use when albums are getting split")
    print("       across folders because tracks have different labels.")
    print("    q. back to menu")
    print()
    audit_keys = list(AUDITS.keys())
    audit_labels = [AUDITS[k][0] for k in audit_keys]

    try:
        choice = input("  > ").strip().lower()
    except EOFError:
        return
    if not choice or choice == "q":
        return

    _log("cmd_audit raw choice=%r", choice)

    # ----- BROKEN-MARKER PASS -----
    # The user's rule: "anything with fucked up metadata gets moved to
    # broken." This option scans the DB and flips the status flag —
    # it doesn't move files itself, because the existing organise pass
    # already routes status='broken' rows to the Broken folder. So:
    #   1. Run this to mark statuses
    #   2. Run option 2 (Organise) to actually move them
    if choice == "b":
        try:
            from audit import mark_broken_metadata
        except ImportError as e:
            print(f"  ✗ couldn't load mark_broken_metadata: {e}")
            return
        print()
        print("  Scanning DB for files missing artist / album / title…")
        # Dry-run first so the user sees what will change.
        n_broken, n_unbroken = mark_broken_metadata(db, dry_run=True)
        print(f"  Would mark broken : {n_broken:,}")
        print(f"  Would un-break    : {n_unbroken:,}")
        print("  (un-break means: row was marked broken before, now has all")
        print("   three required tags. Only un-breaks if the original break")
        print("   reason was metadata-related — copy/quarantine breaks stay.)")
        print()
        if n_broken == 0 and n_unbroken == 0:
            print("  Nothing to do. All rows already have the right status.")
            return
        if not _ask_yn("  Apply these status changes?", default=False):
            print("  Cancelled. No changes were made.")
            return
        n_broken, n_unbroken = mark_broken_metadata(db, dry_run=False)
        print()
        print(f"  ✓ marked broken : {n_broken:,}")
        print(f"  ✓ un-broken     : {n_unbroken:,}")
        print()
        print("  Next: run option 2 (Organise) to actually move the newly-")
        print("  broken files into the Broken folder.")
        return

    # ----- RECONCILE ALBUM FIELDS -----
    # Groups all DB rows by (parent_folder, album). For each group,
    # checks whether label/year/genre have a single agreed-upon value
    # across the tracks that DO have the field set, and fills any
    # tracks where the field is currently empty/placeholder. Conflicts
    # are auto-resolved by majority vote (with the resolution logged)
    # so albums don't get split across folders by the organiser.
    if choice == "r":
        try:
            from audit import reconcile_album_fields
        except ImportError as e:
            print(f"  ✗ couldn't load reconcile_album_fields: {e}")
            return
        print()
        print("  Reconciling album-level fields (label, year, genre)…")
        print("  Grouping by (folder, album). Fills empty/placeholder slots")
        print("  only — existing real values are kept. When tracks have")
        print("  conflicting values for an album-level field, the majority")
        print("  is filled into the empty slots (ties broken alphabetically).")
        print()
        # Dry-run first so the user sees what would change AND any conflicts.
        n_groups, n_cells, conflicts = reconcile_album_fields(db, dry_run=True)
        print(f"  Would fill : {n_cells:,} cells across {n_groups:,} album groups")
        print(f"  Conflicts  : {len(conflicts):,}  (albums with disagreeing values)")
        print()
        if conflicts:
            print("  Conflicts found (first 10 shown):")
            for line in conflicts[:10]:
                print(line)
            if len(conflicts) > 10:
                print(f"    … and {len(conflicts) - 10} more")
            print()
            print("  Each conflict line ends with 'kept X' showing which value")
            print("  was chosen as the album-level value to fill empty slots.")
            print("  Existing per-track values are NOT overwritten by this — to")
            print("  force every track to share one label, use option 'l' instead.")
            print()
        if n_cells == 0:
            print("  Nothing to fill.")
            return
        if not _ask_yn("  Apply these fills?", default=True):
            print("  Cancelled. No changes were made.")
            return
        n_groups, n_cells, _ = reconcile_album_fields(db, dry_run=False)
        print()
        print(f"  ✓ filled {n_cells:,} cells across {n_groups:,} groups")
        return

    # ----- UNIFY RECORD LABELS -----
    # Dedicated "make every track in an album share ONE label" pass.
    # Unlike option 'r' above, this OVERWRITES differing per-track
    # labels with the album's majority value. Use this when albums
    # are getting split across folders by the organiser due to
    # mismatched label tags.
    if choice == "l":
        try:
            from audit import unify_record_labels
        except ImportError as e:
            print(f"  ✗ couldn't load unify_record_labels: {e}")
            return
        print()
        print("  Unify record labels: force every track in an album to share")
        print("  ONE label. Majority vote wins; ties broken alphabetically.")
        print()
        print("  ⚠  This OVERWRITES per-track labels. If track 3 of an album")
        print("     says label='Rephlex' and tracks 1-2 say 'Warp', track 3")
        print("     will be changed to 'Warp'. The Rephlex value is lost.")
        print()
        print("  Why you'd want this: the organiser puts {label}/{album} in")
        print("  the folder path. If two tracks disagree on the label, the")
        print("  album splits across two folders. This unifies them so the")
        print("  album stays together.")
        print()
        # Dry-run first so the user sees every conflict resolution before
        # committing — this is a destructive op, the preview matters.
        n_groups, n_cells, conflicts = unify_record_labels(db, dry_run=True)
        print(f"  Would unify : {n_cells:,} tracks across {n_groups:,} albums")
        print(f"  Conflicts   : {len(conflicts):,}  (albums where tracks disagreed)")
        print()
        if conflicts:
            print("  Conflict resolutions (first 20 shown):")
            for line in conflicts[:20]:
                print(line)
            if len(conflicts) > 20:
                print(f"    … and {len(conflicts) - 20} more")
            print()
        if n_cells == 0:
            print("  Nothing to do — every album already has a unified label.")
            return
        if not _ask_yn("  Apply these label unifications?", default=False):
            print("  Cancelled. No changes were made.")
            return
        n_groups, n_cells, _ = unify_record_labels(db, dry_run=False)
        print()
        print(f"  ✓ unified labels on {n_cells:,} tracks across {n_groups:,} albums")
        print()
        print("  Next: run option 2 (Organise) so the now-consistent labels")
        print("  put every track of each album in the same folder.")
        return

    is_all = choice in ("a", "all")
    chosen_key: str | None = None

    if not is_all:
        # Try number first (1-based index into audit_keys).
        if choice.isdigit():
            try:
                idx = int(choice)
                if 1 <= idx <= len(audit_keys):
                    chosen_key = audit_keys[idx - 1]
            except ValueError:
                pass
        if chosen_key is None:
            # Try by audit key name (e.g. 'missing_artist'), with full
            # resolve_selection semantics (prefix/substring/etc).
            key_resolved, key_ambig = resolve_selection(choice, audit_keys)
            if key_resolved is not None:
                chosen_key = key_resolved
            elif key_ambig:
                print(f"  '{choice}' is ambiguous — matches: {', '.join(key_ambig[:8])}")
                return
            else:
                # Last try: substring against human labels.
                label_resolved, label_ambig = resolve_selection(choice, audit_labels)
                if label_resolved is not None:
                    chosen_key = audit_keys[audit_labels.index(label_resolved)]
                elif label_ambig:
                    print(f"  '{choice}' is ambiguous — matches: {', '.join(label_ambig[:8])}")
                    return
                else:
                    print(f"  no audit matches '{choice}'. Try a number 1-{len(audit_keys)}, an audit key, or 'a' for all.")
                    return
        _log("cmd_audit resolved choice=%r -> %s", choice, chosen_key)

    # Format pick
    print()
    print("  Format: 1) CSV  2) JSON  3) TXT (plain paths) — number or name")
    try:
        fmt_choice = input("  format > ").strip().lower()
    except EOFError:
        return
    # Accept '1', 'csv', '2', 'json', '3', 'txt'
    fmt_resolved, _ = resolve_selection(fmt_choice or "1", ["csv", "json", "txt"])
    fmt = fmt_resolved or "csv"

    if is_all:
        default_dir = str(Path.home() / "music-organiser-audits")
        try:
            out_dir = input(f"  output directory [{default_dir}]: ").strip() or default_dir
        except EOFError:
            out_dir = default_dir
        written = dump_full_report(report, out_dir, fmt=fmt)
        print()
        print(f"  Wrote {len(written)} file(s) to {out_dir}:")
        for p in written:
            print(f"    {p.name}")
        return

    rows = by_key.get(chosen_key, [])
    if not rows:
        print(f"  no rows in '{chosen_key}' — nothing to export")
        return

    from datetime import datetime
    default_path = str(
        Path.home() / f"audit_{chosen_key}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{fmt}"
    )
    try:
        out_path = input(f"  output file [{default_path}]: ").strip() or default_path
    except EOFError:
        out_path = default_path
    written = dump_report(rows, out_path, fmt=fmt)
    print(f"  wrote {len(rows)} rows to {written}")


# =============================================================================
# PAGINATED MULTI-SELECT
# =============================================================================
#
# Used by cmd_fetch_metadata (and any future feature that needs the user
# to pick a set of tag fields). Shows a numbered list, lets the user
# toggle items by number/name, navigate pages, and confirm.

def paginated_multi_select(
    items: list[tuple[str, str]],
    *,
    title: str = "Select items",
    page_size: int = 0,
    initial_selected: set[str] | None = None,
) -> set[str] | None:
    """
    Show a paginated list of (key, label) tuples. The user can toggle
    items in/out of the selected set by typing numbers (e.g. "1 3 5"),
    names (full or unique-prefix), navigate pages with n/p, select all
    with `a`, clear all with `c`, accept with empty/enter, or quit
    with q.

    Returns the final selected set of keys, or None if the user
    cancelled.

    page_size: if 0 (default), auto-sized from the terminal height —
      we leave 12 lines for header, commands and prompt and use the
      rest. Falls back to 20 if we can't detect the terminal.
    """
    selected: set[str] = set(initial_selected or set())
    # Auto-size page from terminal height when caller didn't override
    if not page_size or page_size <= 0:
        try:
            import shutil as _sh
            cols, lines = _sh.get_terminal_size(fallback=(80, 32))
            page_size = max(10, lines - 12)
        except Exception:
            page_size = 20
    page = 0
    total_pages = max(1, (len(items) + page_size - 1) // page_size)

    while True:
        # Clear-ish: just print a separator. We don't try to redraw the
        # whole screen because that fights with terminal scrollback.
        print()
        print(f"  ── {title} ── page {page + 1} of {total_pages} ──"
              f"  ({len(selected)} selected)")
        start = page * page_size
        end = min(start + page_size, len(items))
        for i in range(start, end):
            key, label = items[i]
            mark = "[x]" if key in selected else "[ ]"
            print(f"    {mark} {i + 1:>3}. {key:<20s}  {label}")
        print()
        print("  Commands:")
        print("    <numbers or names>  toggle items (e.g. '1 3 5' or 'artist album')")
        print("    n / p               next / prev page")
        print("    a                   select ALL items")
        print("    c                   clear selection")
        print("    enter               accept selection and continue")
        print("    q                   cancel")

        try:
            raw = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not raw:
            return selected
        low = raw.lower()
        if low in ("q", "quit", "exit", "cancel"):
            return None
        if low == "n":
            if page < total_pages - 1:
                page += 1
            else:
                print("  (already on last page)")
            continue
        if low == "p":
            if page > 0:
                page -= 1
            else:
                print("  (already on first page)")
            continue
        if low == "a":
            selected = {k for k, _ in items}
            print(f"  ✓ selected all {len(selected)} items")
            continue
        if low == "c":
            selected.clear()
            print("  ✓ cleared selection")
            continue

        # Try parsing as space-separated tokens: numbers and names mixed
        tokens = raw.split()
        keys_only = [k for k, _ in items]
        toggled = 0
        unknown = []
        for tok in tokens:
            tl = tok.lower()
            target_key: str | None = None
            if tl.isdigit():
                idx = int(tl)
                if 1 <= idx <= len(items):
                    target_key = items[idx - 1][0]
            else:
                resolved, ambig = resolve_selection(tl, keys_only)
                if resolved is not None:
                    target_key = resolved
                elif ambig:
                    print(f"  '{tok}' is ambiguous: {', '.join(ambig[:5])}")
                    continue
            if target_key is None:
                unknown.append(tok)
                continue
            # Toggle
            if target_key in selected:
                selected.discard(target_key)
            else:
                selected.add(target_key)
            toggled += 1
        if unknown:
            print(f"  ! couldn't resolve: {' '.join(unknown)}")
        if toggled:
            print(f"  ✓ toggled {toggled} item(s) — selection now has {len(selected)}")


# =============================================================================
# FETCH METADATA FROM MUSICBRAINZ
# =============================================================================

def cmd_fetch_metadata(cfg, db) -> None:
    """
    Populate metadata from external providers.

    Three-tier flow modelled on the user's request:

        PopulateMetadata
        > list of common tags                  (artist, album, year, …)
        > other (technical)                    (BPM, ISRC, MB ID, …)
        > raw list of tag names found in db    (every DB column)

    For each unique (artist, album), queries every enabled provider in
    sequence, merges responses (highest score wins per text field), and
    updates the DB.

    BPM gets special treatment: when multiple providers have a value,
    consensus (mode → median tiebreak) picks the canonical one. This
    overrules bogus DAW-estimated BPMs.

    DOES NOT write tags back to audio files. Use option 5 (OneTagger)
    after this for filesystem write-back. Cover art IS written into
    each album folder as `folder.jpg`.
    """
    if not _check_writable(cfg):
        return
    _enter_subscreen(cfg)
    from metadata_lookup import FILLABLE_TAGS, fill_missing_metadata, tag_display_name
    from metadata_providers import ALL_PROVIDERS, make_provider

    print()
    print("  Populate metadata from external providers")
    print("  " + "─" * 50)
    print()

    # -----------------------------------------------------------------------
    # 1. Provider selection
    # -----------------------------------------------------------------------
    print("  Available providers:")
    for cls in ALL_PROVIDERS:
        inst = cls()
        auth = " (needs auth)" if inst.requires_auth else ""
        print(f"    {inst.id:14s} {inst.name}{auth}  "
              f"  ~{inst.rate_limit_seconds}s/request")
    print()
    print("  Type provider IDs separated by spaces (e.g. 'musicbrainz deezer'),")
    # Last-used selection (saved from previous run) -> default
    last_providers = load_last_used(cfg, "fetch_metadata", "providers",
                                     fallback=["musicbrainz", "deezer"])
    if last_providers:
        last_str = " ".join(last_providers)
        print(f"  'a' for all, or press enter for: {last_str}")
    else:
        print("  'a' for all, or press enter for MusicBrainz + Deezer (recommended).")
    try:
        prov_raw = input("  providers > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if prov_raw == "a" or prov_raw == "all":
        provider_ids = [cls.id for cls in ALL_PROVIDERS]
    elif not prov_raw:
        provider_ids = list(last_providers) if last_providers else ["musicbrainz", "deezer"]
    else:
        all_ids = [cls.id for cls in ALL_PROVIDERS]
        provider_ids = []
        for token in prov_raw.split():
            resolved, ambig = resolve_selection(token, all_ids)
            if resolved:
                provider_ids.append(resolved)
            elif ambig:
                print(f"  ! '{token}' is ambiguous: {', '.join(ambig)}")
            else:
                print(f"  ! unknown provider: {token}")
        if not provider_ids:
            print("  no valid providers selected; aborting.")
            return

    # Persist the choice for next time
    save_last_used(cfg, "fetch_metadata", "providers", provider_ids)

    # Instantiate + configure (config-on-demand for auth)
    providers = []
    for pid in provider_ids:
        prov = make_provider(pid)
        if prov is None:
            continue
        try:
            ok = prov.configure(cfg, lambda **kw: cfg_ask(cfg, **kw))
        except Exception as e:
            print(f"  ! {pid} configure failed: {e}")
            continue
        if ok:
            providers.append(prov)
        else:
            print(f"  ! {pid} skipped (configure declined)")
    if not providers:
        print("  no providers configured; aborting.")
        return
    print(f"  ✓ using: {', '.join(p.id for p in providers)}")

    # -----------------------------------------------------------------------
    # 2. Three-tier tag menu
    # -----------------------------------------------------------------------
    # Common: the everyday-album tags
    common_tags = [
        ("artist",         "Artist"),
        ("album",          "Album name"),
        ("year",           "Year of release"),
        ("label",          "Record label"),
        ("genre",          "Genre"),
    ]
    # Technical: catalog ids, BPM, MB ID — the obscure but useful ones
    technical_tags = [
        ("catalog_number", "Catalogue number"),
        ("country",        "Release country"),
        ("barcode",        "Barcode (UPC/EAN)"),
        ("bpm",            "BPM (multi-source consensus)"),
        ("mb_release_id",  "MusicBrainz release ID"),
    ]

    print()
    # ---- v0.18 preset modes — Picard-aligned, four discrete options ----
    # See detection.py / metadata_lookup.py for usage; each preset is a
    # named field set. FULL is the user-requested archival default.

    # Compact list, Picard-aligned where the field names diverge from
    # ours. Display labels are colon-prefixed Picard names for clarity.
    quick_cols = [
        "artist", "album", "title", "year", "genre",
    ]
    thorough_cols = [
        "artist", "albumartist", "album", "title", "year",
        "label", "catalog_number", "genre",
        "country", "barcode",
        "musicbrainz_albumid", "musicbrainz_artistid",
    ]
    full_cols = [
        # Core
        "artist", "albumartist", "artists", "albumartistsort", "artistsort",
        "album", "albumsort", "title", "titlesort", "track_number", "disc_number",
        "totaltracks", "totaldiscs", "discsubtitle",
        # Dates
        "year", "originaldate", "originalyear", "releasedate",
        # Release identity
        "label", "catalog_number", "country", "barcode", "asin",
        "release_type", "release_status", "media_format", "packaging",
        "language", "script",
        # Genre & content
        "genre", "mb_genres", "tags", "annotation",
        # Performance / production credits
        "composer", "composersort", "lyricist", "arranger", "conductor",
        "djmixer", "engineer", "mixer", "producer", "remixer", "writer",
        # Classical
        "work", "movement", "movementnumber", "movementtotal", "showmovement",
        # DJ / technical
        "bpm", "musical_key", "isrc",
        # MB / Discogs IDs
        "musicbrainz_albumid", "musicbrainz_albumartistid", "musicbrainz_artistid",
        "musicbrainz_recordingid", "musicbrainz_workid",
        "musicbrainz_releasegroupid", "musicbrainz_labelid",
        "musicbrainz_originalalbumid", "musicbrainz_originalartistid",
        "musicbrainz_composerid",
        "discogs_release_id", "discogs_master_id",
        "acoustid_id", "acoustid_fingerprint",
        # Originals (re-releases)
        "originalalbum", "originalartist",
        # External links + ancillary
        "url_relations", "website", "copyright", "license",
        "aliases",
    ]

    last_tier = load_last_used(cfg, "fetch_metadata", "tier", fallback="3")
    print("  Tag fill mode:")
    print(f"    1. QUICK     — just the basics ({len(quick_cols)} tags): artist, album, title, year, genre")
    print(f"    2. THOROUGH  — common + IDs ({len(thorough_cols)} tags)")
    print(f"    3. FULL      — archival, everything MB offers ({len(full_cols)} tags) [default]")
    print(f"    4. CUSTOM    — pick fields manually from the raw DB schema")
    print(f"    [default = {last_tier}]")
    print("    q. cancel")
    try:
        choice = input("  > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if choice == "q":
        return
    if choice == "":
        choice = str(last_tier)
    save_last_used(cfg, "fetch_metadata", "tier", choice)

    items: list[tuple[str, str]]
    if choice == "1":
        # QUICK: confirm + go, no per-tag picker for speed
        selected_cols = list(quick_cols)
        items = [(c, c.replace("_", " ").title()) for c in selected_cols]
        default_selected = set(selected_cols)
        skip_picker = True
    elif choice == "2":
        # THOROUGH: same — go straight through
        selected_cols = list(thorough_cols)
        items = [(c, c.replace("_", " ").title()) for c in selected_cols]
        default_selected = set(selected_cols)
        skip_picker = True
    elif choice == "3":
        # FULL: archival mode. Go straight through with everything selected.
        selected_cols = list(full_cols)
        items = [(c, c.replace("_", " ").title()) for c in selected_cols]
        default_selected = set(selected_cols)
        skip_picker = True
    elif choice == "4":
        # CUSTOM: enumerate every column in the `files` table. Filter out
        # columns that don't make sense for external lookup (path, mtime,
        # content_hash, status — these are local-only).
        try:
            cur = db.conn.execute("PRAGMA table_info(files)")
            cols_all = [r["name"] for r in cur.fetchall()]
        except Exception as e:
            print(f"  ! couldn't read DB schema: {e}")
            return
        # Block columns that are filesystem-local
        BLOCK = {
            "path", "mtime", "size_bytes", "content_hash", "source_root",
            "organised_path", "status", "imported_at",
            "broken_reason", "is_high_quality",
            "transcode_checked", "transcode_suspected", "transcode_cutoff_hz",
            "transcode_confidence", "transcode_notes",
            "first_seen", "last_seen",
        }
        # Prefer the user's last-used custom selection if any
        last_custom = load_last_used(cfg, "fetch_metadata", "custom_cols", fallback=None)
        # Pretty display names — "musicbrainz_albumid" → "MusicBrainz album ID"
        # rather than the previous title-cased underscore mangle.
        items = [(c, tag_display_name(c)) for c in cols_all if c not in BLOCK]
        default_selected = set(last_custom) if last_custom else set()
        skip_picker = False
        if not items:
            print("  no fillable columns found in DB. (DB might be empty.)")
            return
    else:
        print("  invalid choice; aborting.")
        return

    if skip_picker:
        # Show what we picked and let the user say "I'd rather tweak it"
        print(f"  Preset will fill {len(default_selected)} tag(s).")
        if _ask_yn("Tweak the tag list before continuing?", default=False):
            skip_picker = False

    if skip_picker:
        selected = default_selected
    else:
        print()
        selected = paginated_multi_select(
            items,
            title="Toggle tags to fill",
            page_size=15,
            initial_selected=default_selected,
        )
        if selected is None:
            print("  cancelled.")
            return
        if not selected:
            print("  nothing selected; aborting.")
            return
        # Remember the custom selection for next time
        if choice == "4":
            save_last_used(cfg, "fetch_metadata", "custom_cols",
                           sorted(list(selected)))
    # Human-readable preview of what's about to be filled
    pretty_preview = [tag_display_name(c) for c in sorted(list(selected))[:8]]
    print(f"  Will fill: {', '.join(pretty_preview)}"
          + ("..." if len(selected) > 8 else "")
          + f"  ({len(selected)} tags)")

    # -----------------------------------------------------------------------
    # 3. Behaviour choices (each remembers last-used)
    # -----------------------------------------------------------------------
    print()
    print()
    print("  How should existing tags be handled?")
    print("    1. ADD ONLY — query every album, preserve existing values, fill gaps only [default, safe]")
    print("    2. OVERWRITE — query every album, providers win; existing values get replaced")
    print("    3. SKIP COMPLETE — only query albums that are MISSING key fields (much faster)")
    print()
    print("    1 vs 3: same write behaviour, but 3 SKIPS the network query for albums where")
    print("    artist+album+year+label are all already populated. Saves time on a mostly-complete")
    print("    library. 2 makes the most requests — re-queries everything and re-writes.")
    last_replace_mode = load_last_used(cfg, "fetch_metadata", "replace_mode", fallback="1")
    print(f"    [default = {last_replace_mode}]")
    try:
        rm = input("  > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if rm == "":
        rm = last_replace_mode
    # only_missing applies to FIELD-level merge (per existing tag).
    # skip_complete applies to ALBUM-level filtering (whether we query the
    # album at all). They're independent dimensions:
    #   mode 1 = only_missing=True,  skip_complete=False  (default)
    #   mode 2 = only_missing=False, skip_complete=False
    #   mode 3 = only_missing=True,  skip_complete=True   (NEW: fast pass)
    only_missing = (rm in ("1", "3", "add", "add only", "missing", "skip"))
    skip_complete = (rm in ("3", "skip", "skip complete"))
    save_last_used(cfg, "fetch_metadata", "replace_mode", rm if rm in ("1","2","3") else "1")

    fetch_covers = ask_yn_with_last_used(
        cfg, "fetch_metadata", "fetch_covers",
        "Also download front cover art (folder.jpg per album)?",
        default=True,
    )
    resolve_bpm = "bpm" in selected and ask_yn_with_last_used(
        cfg, "fetch_metadata", "resolve_bpm",
        "Run BPM consensus lookup (per-track, slower)?",
        default=True,
    )
    write_to_files = ask_yn_with_last_used(
        cfg, "fetch_metadata", "write_to_files",
        "Write tags INTO the audio files (not just the DB)? "
        "(needed for Soulseek seeding etc)",
        default=True,
    )
    touch_verified_rips = False
    if write_to_files:
        touch_verified_rips = ask_yn_with_last_used(
            cfg, "fetch_metadata", "touch_verified_rips",
            "Also retag files in folders with EAC/XLD logs? "
            "(default N — preserves verified-rip integrity)",
            default=False,
        )
    write_nfo = ask_yn_with_last_used(
        cfg, "fetch_metadata", "write_nfo",
        "Generate album.nfo info files per album folder?",
        default=True,
    )
    # Commit-per-file: forces each upsert to fsync to disk immediately
    # rather than batching via the SQLite WAL. PROS: crash mid-fetch
    # loses zero rows. CONS: ~50ms-2s extra per file on USB-attached
    # storage (the typical environment for this script). The WAL is
    # already crash-safe via atomic-append journalling, so this is
    # genuinely a paranoid-mode toggle, not a safety necessity.
    commit_per_file = ask_yn_with_last_used(
        cfg, "fetch_metadata", "commit_per_file",
        "Commit every row to disk immediately (slower but crash-safe)? "
        "(N is fine — SQLite's WAL handles power-loss anyway)",
        default=False,
    )

    # -----------------------------------------------------------------------
    # 4. Library size estimate
    # -----------------------------------------------------------------------
    print()
    n_files = db.count()
    try:
        n_albums = db.conn.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT artist, album FROM files "
            "WHERE artist IS NOT NULL AND album IS NOT NULL "
            "AND artist != '' AND album != '')"
        ).fetchone()[0]
    except Exception:
        n_albums = 0

    # ----- HONEST ETA -----
    # The old estimate was n_albums × sum(provider_rates). That's the
    # absolute worst case where EVERY album queries EVERY provider in
    # series. Real runs look very different:
    #
    #   - Providers run serially per album but stop at the first hit.
    #     If provider #1 returns results, providers #2..N never fire.
    #     For a typical library, MB hits ~30-60% of albums (more for
    #     mainstream, less for underground D&B).
    #   - skip_complete drops entire albums with no network cost.
    #   - quick_check_album skips unofficial/bootleg with no cost.
    #   - Resume-from-checkpoint skips already-processed albums.
    #   - Fallback queries (cat-stripped, VA, series-only) cost 1
    #     extra request each WHEN PRIMARY MISSES. So a missed album
    #     can cost 2-5× a hit.
    #
    # Realistic-range estimate: best/likely/worst based on
    # configurable per-provider hit-rates plus fallback expansion.
    #
    # Honest about its own accuracy: this is still a static projection
    # not informed by your library's actual hit rate. The first 100
    # albums of the run will tell us the real rate; from there the
    # in-run ETA (now EMA-smoothed) is what to trust.

    # Per-provider rate-limit cost (seconds per request)
    rates = [(p.id, p.rate_limit_seconds) for p in providers]
    # First-provider-only (best case): the fastest one always hits.
    best_per_album = min(r for _, r in rates) if rates else 0.0
    # All providers serially (worst case): nothing matches anywhere
    # AND every fallback query expands. Rough multiplier: ~2x for
    # the median album that triggers cat-strip OR VA fallback.
    worst_per_album = sum(r for _, r in rates) * 2.0
    # Likely: ~60% hit-rate on MB (the first provider) + remainder
    # falls through to ~1 extra provider on average.
    likely_per_album = best_per_album * 0.6 + sum(r for _, r in rates) * 0.4

    def _fmt(min_total):
        if min_total < 60:
            return f"{min_total:.1f} minutes"
        if min_total < 60 * 24:
            return f"{min_total / 60:.1f} hours"
        return f"{min_total / 60 / 24:.1f} days"

    # Already-processed albums from a resumable checkpoint: shave them
    # off the album count if a matching checkpoint is on disk.
    n_albums_effective = n_albums
    resume_skip_count = 0
    try:
        from checkpoint import load_fetch_checkpoint, fetch_checkpoint_matches
        existing_cp = load_fetch_checkpoint()
        if existing_cp is not None and existing_cp.phase != "complete":
            same_op = fetch_checkpoint_matches(
                existing_cp,
                db_path=str(db.path),
                target_columns=list(selected),
                provider_ids=[p.id for p in providers],
                only_missing=only_missing,
                write_to_files=write_to_files,
            )
            if same_op:
                resume_skip_count = len(existing_cp.processed_albums)
                n_albums_effective = max(0, n_albums - resume_skip_count)
    except Exception:
        pass

    print(f"  Library: {n_files:,} files, {n_albums:,} unique albums.")
    if resume_skip_count:
        print(f"  Resume:  {resume_skip_count:,} already processed → "
              f"{n_albums_effective:,} to query this run")
    print(f"  Providers: {len(providers)} active ({', '.join(p.id for p in providers)})")
    if n_albums_effective and rates:
        best_min   = (n_albums_effective * best_per_album) / 60
        likely_min = (n_albums_effective * likely_per_album) / 60
        worst_min  = (n_albums_effective * worst_per_album) / 60
        print(f"  Estimated runtime range (rough — real rate will be")
        print(f"  established within the first ~100 albums):")
        print(f"      best   ~{_fmt(best_min)}    (if first provider hits ~every album)")
        print(f"      likely ~{_fmt(likely_min)}    (typical ~60% MB hit rate)")
        print(f"      worst  ~{_fmt(worst_min)}    (everything falls through every provider)")
        print(f"  After the run starts, the live ETA in the UI is what to trust —")
        print(f"  it uses EMA-smoothed rate from the last ~60 seconds of activity.")
    print()

    if not _ask_yn("Proceed?", default=True):
        return

    from ui import make_ui
    ui_cfg = cfg.get("ui", {})
    ui = make_ui(
        theme=ui_cfg.get("theme", "rainbowdash"),
        mode_label="Populate",
        use_rich=ui_cfg.get("use_rich", True),
        refresh_per_second=ui_cfg.get("refresh_per_second", 20),
        force_terminal=ui_cfg.get("force_terminal", False),
        color_system=ui_cfg.get("color_system", "auto"),
    )

    # ----- RESUME PROMPT -----
    # Check for a leftover checkpoint and offer to resume. The user
    # gets to choose: continue from where the last run left off
    # (skip already-processed albums), OR start fresh (re-query
    # everything, which on a 200k-album library is hours of wasted
    # work).
    resume = True
    try:
        from checkpoint import (
            load_fetch_checkpoint, fetch_checkpoint_matches,
            describe_fetch_checkpoint, clear_fetch_checkpoint,
        )
        existing_cp = load_fetch_checkpoint()
        if existing_cp is not None and existing_cp.phase != "complete":
            same_op = fetch_checkpoint_matches(
                existing_cp,
                db_path=str(db.path),
                target_columns=list(selected),
                provider_ids=[p.id for p in providers],
                only_missing=only_missing,
                write_to_files=write_to_files,
            )
            if same_op:
                print()
                print("  Found an unfinished fetch run from earlier:")
                print(describe_fetch_checkpoint(existing_cp))
                print()
                ans = input("  Resume (skip already-processed albums)? [Y/n] ").strip().lower()
                if ans in ("n", "no"):
                    resume = False
                    try:
                        clear_fetch_checkpoint()
                    except Exception:
                        pass
                else:
                    resume = True
            else:
                # Stale checkpoint from a different operation — clear it
                # so it doesn't sit forever. The user didn't ask us to
                # resume it; we shouldn't keep it around to confuse them.
                print()
                print("  Found a fetch checkpoint, but it's from a DIFFERENT")
                print("  operation (different DB / providers / columns / flags).")
                print("  Clearing it so this run starts fresh.")
                try:
                    clear_fetch_checkpoint()
                except Exception:
                    pass
    except Exception as e:
        # If checkpoint check fails (corrupt JSON, etc), proceed without
        # resume — the worst case is a full re-run, which is what would
        # have happened without checkpoints anyway.
        print(f"  (checkpoint check skipped: {e})")

    # Confidence-gating params — read from [fetch_metadata] section.
    # Sensible defaults if the config doesn't have them yet (e.g. user
    # upgraded without re-running setup).
    fm_cfg = cfg.get("fetch_metadata", {}) or {}
    min_conf = float(fm_cfg.get("min_confidence_score", 80.0))
    amb_margin = float(fm_cfg.get("ambiguity_margin", 5.0))
    pp_thresholds = fm_cfg.get("thresholds", {}) or {}
    # Coerce to {str: float} since TOML may store ints
    pp_thresholds = {str(k): float(v) for k, v in pp_thresholds.items()}

    try:
        with ui:
            stats = fill_missing_metadata(
                db,
                providers=providers,
                target_columns=list(selected),
                fetch_covers=fetch_covers,
                only_missing=only_missing,
                skip_complete_albums=skip_complete,
                resolve_bpm=resolve_bpm,
                write_to_files=write_to_files,
                touch_verified_rips=touch_verified_rips,
                commit_per_file=commit_per_file,
                resume=resume,
                db_path=str(db.path),
                min_confidence_score=min_conf,
                ambiguity_margin=amb_margin,
                per_provider_thresholds=pp_thresholds,
                ui=ui,
            )
    except KeyboardInterrupt:
        print()
        print("  ✗ interrupted. partial progress preserved in the DB.")
        print("  Run option 7 again to resume from where you left off —")
        print("  the checkpoint at ~/.cache/music-organiser/fetch_checkpoint.json")
        print("  remembers which albums were already processed.")
        return

    print()
    print(stats.summary())
    if stats.errors:
        print(f"\n  First 10 errors (of {len(stats.errors)}):")
        for e in stats.errors[:10]:
            print(f"    {e}")

    # NFO generation runs after metadata is settled. Uses the now-updated
    # DB rows so it picks up everything we just fetched. Skips folders
    # that already have an album.nfo unless overwrite=True (not exposed
    # in this menu — set the config field directly to override).
    if write_nfo:
        print()
        print("  Generating album.nfo files…")
        try:
            from nfo_writer import write_album_nfos_for_db
        except ImportError as e:
            print(f"  ✗ Cannot load nfo_writer: {e} — skipping NFO generation")
            write_nfo = False
    if write_nfo:
        overwrite = bool(cfg.get("nfo", {}).get("overwrite", False))
        nfo_stats = write_album_nfos_for_db(
            db,
            style=cfg.get("nfo", {}).get("style", "nfo"),
            overwrite=overwrite,
        )
        print(f"  NFO: wrote {nfo_stats['written']:,}, "
              f"skipped existing {nfo_stats['skipped_exists']:,}, "
              f"no-folder {nfo_stats['skipped_no_folder']:,}, "
              f"errors {nfo_stats['errors']:,}")


# =============================================================================
# ONETAGGER
# =============================================================================

def cmd_onetagger(cfg, db) -> None:
    """
    Fix broken tags using OneTagger.

    Workflow:
      1. Ask what's broken (missing artist, missing title, placeholder
         artist, transcode suspect, all of the above...).
      2. Build a folder list from the DB matching that category.
      3. Either launch OneTagger on each, save the list to a file for
         later batch-fix, or write a M3U playlist OneTagger can chew
         through.
      4. Tell the user: after fixing, run menu option 2 (re-index) to
         pull the new tags back into the DB.

    OneTagger is detected automatically; if missing we print install hints.
    """
    # Import the integrations.onetagger helper. If this fails it almost
    # always means the user has organiser.py but not the integrations/
    # subfolder next to it — for instance if they only re-downloaded a
    # single file as a hot-patch. Give an actionable message rather than
    # the bare traceback.
    try:
        from integrations.onetagger import (
            find_onetagger, launch_onetagger, install_hint,
        )
    except ModuleNotFoundError as e:
        # Diagnostics: where is THIS file living, what does its folder
        # contain, and what would the missing folder be?
        import os
        here = Path(__file__).resolve().parent
        siblings = sorted(p.name for p in here.iterdir()) if here.exists() else []
        has_integrations_dir = (here / "integrations").is_dir()
        has_init = (here / "integrations" / "__init__.py").is_file()

        print()
        print("  ✗ Can't load the OneTagger integration module.")
        print(f"    ({e})")
        print()
        print(f"  organiser.py lives at: {Path(__file__).resolve()}")
        print(f"  integrations/ folder present here? {has_integrations_dir}")
        print(f"  integrations/__init__.py present? {has_init}")
        print()
        if not has_integrations_dir:
            print("  → The integrations/ subfolder is missing. Most likely cause:")
            print("    you downloaded organiser.py on its own (e.g. as a hot-")
            print("    patch) and dropped it into a folder that doesn't have")
            print("    the full bundle.")
            print()
            print("  Fix: download music_organiser.zip again and extract it")
            print(f"  so the layout matches:")
            print(f"    {here}/organiser.py")
            print(f"    {here}/integrations/__init__.py")
            print(f"    {here}/integrations/onetagger.py")
            print()
            print("  Other folders found in your install directory:")
            for s in siblings[:20]:
                kind = "/" if (here / s).is_dir() else ""
                print(f"    {s}{kind}")
        else:
            print("  → integrations/ exists but is broken (missing __init__.py")
            print("    or missing onetagger.py). Re-extract the zip to restore.")
        return

    _enter_subscreen(cfg)

    binary = find_onetagger()
    print()
    if binary is None:
        print("  OneTagger isn't installed (or not found on $PATH or the")
        print("  usual locations).")
        print()
        print(install_hint())
        print()
        print("  You can still use this menu — option 4 will list bad-metadata")
        print("  folders and let you save the list to a file. Once OneTagger")
        print("  is installed, come back here and use option 1 or 2.")
        binary_found = False
    else:
        print(f"  ✓ OneTagger found at: {binary}")
        binary_found = True

    # Show what we know is broken
    print()
    print("  What's broken in the library?")
    issues = _summarise_broken_metadata(db)
    print()
    for label, count in issues.items():
        print(f"    {label:42s} {count:>6,} files / {_distinct_folders(db, label):>5,} folders")
    print()

    print("  How would you like to fix it?")
    print("    1. Pick a category, launch OneTagger on each folder in turn")
    print("    2. Pick a category, save folder list to file (batch later)")
    print("    3. Generate M3U playlist of every broken track (one file)")
    print("    4. List top-50 broken folders (just print to screen)")
    if binary_found:
        print("    5. Launch OneTagger on destination_root (browse manually)")
    print("    q. back to main menu")
    print()
    try:
        choice = input("  > ").strip().lower()
    except EOFError:
        return
    if not choice or choice == "q":
        return

    if choice == "5" and binary_found:
        dest = cfg.get("paths", {}).get("destination_root", "")
        if not dest:
            print("  no destination_root configured")
            return
        proc = launch_onetagger(binary, folder=dest)
        if proc:
            print(f"  launched (pid {proc.pid}) on {dest}")
        else:
            print("  failed to launch")
        return

    # Choices 1, 2, 4 all need a category pick
    if choice in ("1", "2", "4"):
        category = _ask_broken_category()
        if category is None:
            return
        folders = _folders_for_category(db, category)
        if not folders:
            print("  no folders match that category — nothing to fix.")
            return
        print(f"\n  {len(folders)} folders to fix.\n")

        if choice == "4":
            for f in folders[:50]:
                print(f"    {f}")
            if len(folders) > 50:
                print(f"    ... and {len(folders) - 50} more.")
            return

        if choice == "2":
            default_path = str(Path.home() / f"to_fix_{category}.txt")
            try:
                out = input(f"  output file [{default_path}]: ").strip() or default_path
            except EOFError:
                out = default_path
            Path(out).expanduser().write_text("\n".join(folders) + "\n",
                                              encoding="utf-8")
            print(f"  wrote {len(folders)} folders to {out}")
            print()
            print("  When you're ready, run OneTagger and add these folders")
            print("  to its 'autotag' or 'quick tag' source list.")
            print("  Then re-index (main menu option 2) to pull updated tags into the DB.")
            return

        # choice == "1": launch OneTagger on each, one at a time
        if not binary_found:
            print("  OneTagger isn't installed; can't launch.")
            return
        print(f"  Will launch OneTagger on each of {len(folders)} folders in turn.")
        print(f"  After each fix, close OneTagger to advance to the next.")
        print(f"  Type 'all' to launch them all at once instead (each in its own window).")
        try:
            mode = input("  enter / 'all' / 'q' to cancel > ").strip().lower()
        except EOFError:
            return
        if mode == "q":
            return
        if mode == "all":
            for f in folders:
                launch_onetagger(binary, folder=f)
            print(f"  launched {len(folders)} OneTagger processes.")
        else:
            for i, f in enumerate(folders, 1):
                print(f"\n  [{i}/{len(folders)}] {f}")
                proc = launch_onetagger(binary, folder=f, detach=False)
                if proc is None:
                    print("    failed to launch — skipping.")
                    continue
                try:
                    proc.wait()
                except KeyboardInterrupt:
                    proc.terminate()
                    print("  interrupted.")
                    return
        print()
        print("  Done. Run menu option 2 (re-index) to pick up the new tags.")
        return

    if choice == "3":
        # M3U playlist of every track in every bad-metadata folder
        from datetime import datetime
        default_path = str(Path.home() / f"broken_tracks_{datetime.now().strftime('%Y%m%d_%H%M%S')}.m3u")
        try:
            out = input(f"  output M3U file [{default_path}]: ").strip() or default_path
        except EOFError:
            out = default_path

        rows = db.conn.execute("""
            SELECT path, artist, title, duration_seconds
            FROM files
            WHERE status != 'broken' AND (
                artist IS NULL OR TRIM(artist) = ''
                OR album IS NULL OR TRIM(album) = ''
                OR title IS NULL OR TRIM(title) = ''
                OR LOWER(TRIM(artist)) IN ('unknown artist', 'unknown', 'various artists', 'va')
                OR title GLOB '[0-9][0-9]*-*'
                OR transcode_suspected = 1
            )
            ORDER BY artist, path
        """).fetchall()

        with open(Path(out).expanduser(), "w", encoding="utf-8") as fp:
            fp.write("#EXTM3U\n")
            for r in rows:
                dur = int(r["duration_seconds"] or 0)
                artist = r["artist"] or "Unknown"
                title = r["title"] or "Unknown"
                fp.write(f"#EXTINF:{dur},{artist} - {title}\n")
                fp.write(f"{r['path']}\n")
        print(f"  wrote {len(rows)} tracks to {out}")
        return


def _summarise_broken_metadata(db) -> dict[str, int]:
    """Quick counts for the OneTagger menu. Returns label -> file count."""
    counts: dict[str, int] = {}
    c = db.conn
    counts["Missing artist tag"] = c.execute(
        "SELECT COUNT(*) FROM files WHERE (artist IS NULL OR TRIM(artist)='') AND status!='broken'"
    ).fetchone()[0]
    counts["Missing album tag"] = c.execute(
        "SELECT COUNT(*) FROM files WHERE (album IS NULL OR TRIM(album)='') AND status!='broken'"
    ).fetchone()[0]
    counts["Missing title tag"] = c.execute(
        "SELECT COUNT(*) FROM files WHERE (title IS NULL OR TRIM(title)='') AND status!='broken'"
    ).fetchone()[0]
    counts["Placeholder artist ('Unknown' etc)"] = c.execute(
        "SELECT COUNT(*) FROM files WHERE LOWER(TRIM(artist)) IN "
        "('unknown artist','unknown','various artists','va','v.a.','n/a','untagged')"
    ).fetchone()[0]
    counts["Title looks like filename"] = c.execute(
        "SELECT COUNT(*) FROM files WHERE title GLOB '[0-9][0-9]*-*' AND status!='broken'"
    ).fetchone()[0]
    counts["Missing label tag"] = c.execute(
        "SELECT COUNT(*) FROM files WHERE (label IS NULL OR TRIM(label)='') AND status!='broken'"
    ).fetchone()[0]
    counts["Missing year tag"] = c.execute(
        "SELECT COUNT(*) FROM files WHERE (year IS NULL OR TRIM(year)='') AND status!='broken'"
    ).fetchone()[0]
    counts["Suspected fake-FLAC"] = c.execute(
        "SELECT COUNT(*) FROM files WHERE transcode_suspected = 1"
    ).fetchone()[0]
    counts["ANY of the above"] = c.execute("""
        SELECT COUNT(*) FROM files WHERE status != 'broken' AND (
            artist IS NULL OR TRIM(artist) = ''
            OR album IS NULL OR TRIM(album) = ''
            OR title IS NULL OR TRIM(title) = ''
            OR label IS NULL OR TRIM(label) = ''
            OR year IS NULL OR TRIM(year) = ''
            OR LOWER(TRIM(artist)) IN ('unknown artist','unknown','various artists','va')
            OR title GLOB '[0-9][0-9]*-*'
            OR transcode_suspected = 1
        )
    """).fetchone()[0]
    return counts


def _distinct_folders(db, label: str) -> int:
    """Count distinct folders for a given category label. Cheap rough estimate."""
    cat = _label_to_category(label)
    if cat is None:
        return 0
    sql_where = _category_where(cat)
    if sql_where is None:
        return 0
    row = db.conn.execute(
        f"SELECT COUNT(DISTINCT CASE WHEN organised_path IS NOT NULL AND organised_path != '' "
        f"THEN substr(organised_path, 1, length(organised_path) - instr(reverse(organised_path), '/')) "
        f"ELSE substr(path, 1, length(path) - instr(reverse(path), '/')) END) "
        f"FROM files WHERE {sql_where}"
    ).fetchone()
    return row[0] if row else 0


def _label_to_category(label: str) -> str | None:
    mapping = {
        "Missing artist tag": "missing_artist",
        "Missing album tag": "missing_album",
        "Missing title tag": "missing_title",
        "Placeholder artist ('Unknown' etc)": "placeholder_artist",
        "Title looks like filename": "filename_title",
        "Missing label tag": "missing_label",
        "Missing year tag": "missing_year",
        "Suspected fake-FLAC": "transcode",
        "ANY of the above": "any",
    }
    return mapping.get(label)


def _category_where(category: str) -> str | None:
    """SQL WHERE clause body for the given category. Status != 'broken' included."""
    base = "status != 'broken' AND "
    clauses = {
        "missing_artist": "(artist IS NULL OR TRIM(artist) = '')",
        "missing_album":  "(album IS NULL OR TRIM(album) = '')",
        "missing_title":  "(title IS NULL OR TRIM(title) = '')",
        "missing_label":  "(label IS NULL OR TRIM(label) = '')",
        "missing_year":   "(year IS NULL OR TRIM(year) = '')",
        "placeholder_artist": (
            "LOWER(TRIM(artist)) IN "
            "('unknown artist','unknown','various artists','va','v.a.','n/a','untagged')"
        ),
        "filename_title": "title GLOB '[0-9][0-9]*-*'",
        "transcode":      "transcode_suspected = 1",
        "any": (
            "(artist IS NULL OR TRIM(artist) = ''"
            " OR album IS NULL OR TRIM(album) = ''"
            " OR title IS NULL OR TRIM(title) = ''"
            " OR label IS NULL OR TRIM(label) = ''"
            " OR year IS NULL OR TRIM(year) = ''"
            " OR LOWER(TRIM(artist)) IN ('unknown artist','unknown','various artists','va')"
            " OR title GLOB '[0-9][0-9]*-*'"
            " OR transcode_suspected = 1)"
        ),
    }
    c = clauses.get(category)
    if c is None:
        return None
    return base + c


def _ask_broken_category() -> str | None:
    """Prompt user to pick a broken-metadata category. Returns the key."""
    categories = [
        ("any",              "ANY broken metadata"),
        ("missing_artist",   "Missing artist tag only"),
        ("missing_album",    "Missing album tag only"),
        ("missing_title",    "Missing title tag only"),
        ("placeholder_artist", "Placeholder artist ('Unknown' etc)"),
        ("filename_title",   "Title looks like filename"),
        ("missing_label",    "Missing label tag"),
        ("missing_year",     "Missing year tag"),
        ("transcode",        "Suspected fake-FLAC (run Verify FLACs first)"),
    ]
    print()
    print("  Category:")
    for i, (_, label) in enumerate(categories, 1):
        print(f"    {i}. {label}")
    print("    q. cancel")
    print()
    try:
        choice = input("  > ").strip().lower()
    except EOFError:
        return None
    if not choice or choice == "q":
        return None
    try:
        return categories[int(choice) - 1][0]
    except (ValueError, IndexError):
        print("  invalid choice")
        return None


def _folders_for_category(db, category: str) -> list[str]:
    """Get distinct parent folders of files in the given category."""
    where = _category_where(category)
    if where is None:
        return []
    sql = f"""
        SELECT DISTINCT
            CASE WHEN organised_path IS NOT NULL AND organised_path != ''
                 THEN organised_path
                 ELSE path
            END as p
        FROM files
        WHERE {where}
    """
    rows = db.conn.execute(sql).fetchall()
    seen: set[str] = set()
    folders: list[str] = []
    for r in rows:
        p = r["p"]
        if not p:
            continue
        folder = str(Path(p).parent)
        if folder not in seen:
            seen.add(folder)
            folders.append(folder)
    folders.sort()
    return folders


def cmd_show_config(cfg) -> None:
    from config import config_path
    import json
    _enter_subscreen(cfg)
    print(f"\nConfig file: {config_path()}\n")
    print(json.dumps(cfg, indent=2, default=str))


def cmd_speed(cfg) -> None:
    """
    Pick a performance tier. Changes how the script schedules its
    CPU and I/O priorities and how aggressively SQLite buffers writes.

    Reference: Spaceballs. Default is LUDICROUS SPEED.
    """
    from speed import SPEED_LEVELS, apply_speed, set_active_level
    from config import save_config

    _enter_subscreen(cfg)
    current = cfg.get("performance", {}).get("speed_level", "ludicrous")

    print()
    print("  Performance tiers")
    print("  " + "─" * 60)
    print()

    levels_in_order = ["sub-light", "light-speed", "ridiculous", "ludicrous", "plaid"]
    for i, lid in enumerate(levels_in_order, 1):
        lvl = SPEED_LEVELS[lid]
        marker = "  ← current" if lid == current else ""
        root_note = "  (needs sudo)" if lvl.needs_root else ""
        print(f"    {i}. {lvl.display:18s}  nice={lvl.nice:+d}  "
              f"ionice={lvl.ionice_class}:{lvl.ionice_level}  "
              f"sync={lvl.sqlite_sync}{root_note}{marker}")
        # word-wrap the description into ~70 cols
        desc = lvl.description
        words = desc.split()
        line = "       "
        for w in words:
            if len(line) + len(w) + 1 > 76:
                print(line)
                line = "       " + w
            else:
                line = (line + " " + w) if line.strip() else (line + w)
        if line.strip():
            print(line)
        print()

    print("  Type a number or name (sub-light, light-speed, ridiculous,")
    print("  ludicrous, plaid). q to cancel. Default is LUDICROUS.")
    try:
        raw = input("  > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if not raw or raw == "q":
        return

    # Numeric pick
    chosen: str | None = None
    if raw.isdigit():
        idx = int(raw)
        if 1 <= idx <= len(levels_in_order):
            chosen = levels_in_order[idx - 1]
    else:
        # Name / prefix / substring via resolve_selection
        resolved, ambig = resolve_selection(raw, levels_in_order)
        if resolved:
            chosen = resolved
        elif ambig:
            print(f"  '{raw}' is ambiguous: {', '.join(ambig)}")
            return

    if chosen is None:
        print(f"  no tier matches '{raw}'")
        return

    # Persist and apply immediately
    cfg.setdefault("performance", {})["speed_level"] = chosen
    save_config(cfg)
    applied = apply_speed(chosen)
    set_active_level(applied.level)

    print()
    print(f"  ✓ saved performance.speed_level = {chosen}")
    print(f"    {applied.level.display}: nice={applied.actual_nice}  "
          f"ionice={applied.actual_ionice}")
    if applied.warnings:
        for w in applied.warnings:
            print(f"    ⓘ  {w}")
        print(f"    (priority tweaks need root — but they barely matter for")
        print(f"     this workload. running as a normal user is fine.)")
    print()
    print("  Note: SQLite sync changes apply on next launch only.")
    print(f"  DB batch size is {applied.level.db_batch_size:,} rows/transaction.")


def cmd_show_debug_log() -> None:
    """Print the tail of the debug log, then offer to clear it."""
    log_path = get_debug_log_path()
    if log_path is None:
        print()
        print("  No debug log is active (couldn't open one at startup).")
        return
    print()
    print(f"  Debug log: {log_path}")
    if not log_path.exists():
        print("  (log is empty)")
        return
    try:
        size = log_path.stat().st_size
        print(f"  Size: {size:,} bytes")
        print()
        # Show the last ~40 lines, which is usually enough to see recent
        # actions without overwhelming the screen.
        with open(log_path, "r", encoding="utf-8", errors="replace") as fp:
            lines = fp.readlines()
        tail = lines[-40:]
        print("  --- last 40 log lines ---")
        for line in tail:
            print(f"  {line.rstrip()}")
        print("  --- end ---")
    except OSError as e:
        print(f"  couldn't read log: {e}")


def cmd_query(db, cfg=None) -> None:
    """Run a quick ad-hoc SQL SELECT for inspection."""
    if cfg is not None:
        _enter_subscreen(cfg)
    print()
    print("Enter a SELECT statement (or empty to cancel).")
    print("Tip: tables are `files`. Try:")
    print("  SELECT label, COUNT(*) FROM files GROUP BY label ORDER BY 2 DESC LIMIT 20;")
    try:
        sql = input("sql> ").strip()
    except EOFError:
        return
    if not sql:
        return
    if not sql.lower().lstrip().startswith("select"):
        print("Only SELECT is allowed from this menu.")
        return
    try:
        rows = db.conn.execute(sql).fetchall()
    except Exception as e:
        print(f"error: {e}")
        return
    if not rows:
        print("(no rows)")
        return
    cols = rows[0].keys()
    print(" | ".join(cols))
    print("-" * 80)
    for r in rows[:200]:
        print(" | ".join(str(r[c]) if r[c] is not None else "" for c in cols))
    if len(rows) > 200:
        print(f"... ({len(rows) - 200} more rows truncated)")


def cmd_theme(cfg) -> None:
    """List available themes and let the user pick one. Saved to config.

    Sub-commands inside this menu:
      preview <name>    — render a 5-second demo of any theme
      dump              — write all built-in themes to ~/.config/music-organiser/themes/
      dump-force        — overwrite even if files already exist (use to refresh after a
                          built-in palette change)
      reload            — reset the theme cache so edits to .toml files take effect
      source native     — prefer built-in themes (ignore .toml files)
      source external   — prefer user .toml files (auto-dumps natives if first time)
      list              — show every theme + its source (native / external / both)
      open <name>       — print the filesystem path of a theme's .toml file
      q                 — back to main menu
    """
    try:
        from ui import list_themes, make_ui
    except Exception:
        list_themes = lambda: []
    from config import save_config

    _enter_subscreen(cfg)

    # Cached themes module — try once, fall back gracefully if missing
    try:
        import themes as themes_mod
    except ImportError:
        themes_mod = None

    def _refresh_list() -> list[str]:
        """All theme names available right now (native + external merged)."""
        builtins = list(list_themes())
        if themes_mod is None:
            return builtins
        try:
            externals = sorted(themes_mod.list_all_themes().keys())
        except Exception:
            externals = builtins
        # Merge preserving order: built-ins first, then any externals not
        # already covered
        seen = set(externals)
        out = list(externals)
        for b in builtins:
            if b not in seen:
                out.append(b)
                seen.add(b)
        return out

    current = cfg.get("ui", {}).get("theme", "cyber")
    theme_source = cfg.get("ui", {}).get("theme_source", "external")

    def _print_menu() -> list[str]:
        names = _refresh_list()
        print()
        print(f"  Current theme:  {current}")
        print(f"  Source pref:    {theme_source}  "
              f"(swap with 'source native' / 'source external')")
        print()
        if themes_mod is not None:
            try:
                catalog = themes_mod.list_all_themes()
            except Exception:
                catalog = {}
        else:
            catalog = {}
        print("  Available themes:")
        for i, t in enumerate(names, 1):
            marker = "  <- current" if t == current else ""
            source = catalog.get(t, "native")
            tag = {
                "native": "n",
                "external": "e",
                "native+external": "n+e",
            }.get(source, "?")
            print(f"    {i:>2}. {t:<22} [{tag}]{marker}")
        print()
        print("  type a number/name to switch, OR a sub-command:")
        print("    preview <name>     5-sec demo")
        print("    dump               write all natives to your themes/ folder")
        print("    dump-force         overwrite existing files")
        print("    source native      prefer built-in themes (safety mode)")
        print("    source external    prefer your .toml files (allows customisation)")
        print("    list               re-print this menu")
        print("    open <name>        show the path to that theme's .toml file")
        print("    q                  back to main menu")
        return names

    themes_list = _print_menu()

    while True:
        try:
            raw = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not raw or raw.lower() in ("q", "quit", "back"):
            return

        lower = raw.lower()

        # ----- Sub-commands -----
        if lower == "list":
            themes_list = _print_menu()
            continue
        if lower in ("dump", "dump-force"):
            if themes_mod is None:
                print("  themes module unavailable — cannot dump.")
                continue
            try:
                results = themes_mod.dump_builtin_themes(
                    overwrite=(lower == "dump-force")
                )
                written = sum(1 for v in results.values() if v == "written")
                skipped = sum(1 for v in results.values() if v.startswith("skipped"))
                errors  = sum(1 for v in results.values() if v.startswith("error"))
                print(f"  Dumped: {written} written, {skipped} skipped, {errors} errors")
                print(f"  Folder: {themes_mod.themes_dir()}")
                if errors:
                    for n, r in results.items():
                        if r.startswith("error"):
                            print(f"    ! {n}: {r}")
            except Exception as e:
                print(f"  dump failed: {e}")
            themes_list = _refresh_list()
            continue
        if lower == "reload":
            # No persistent cache to flush right now — get_theme reads
            # from disk on every call. Print a note so user understands.
            print("  themes are read fresh from disk on each render; no reload needed.")
            continue
        if lower.startswith("source"):
            parts = raw.split(None, 1)
            if len(parts) < 2:
                print(f"  current source: {theme_source}. usage: source native | source external")
                continue
            new_src = parts[1].strip().lower()
            if new_src not in ("native", "external"):
                print("  source must be 'native' or 'external'")
                continue
            # Switching TO external for the first time: auto-dump natives so
            # the user has something to edit. This matches what the user
            # specifically asked for: "if i swap from native to external,
            # it will create and load rainbowdash as a local theme — which
            # will also trigger the code to dump all theme files"
            if new_src == "external" and themes_mod is not None:
                existing = themes_mod.list_external_themes()
                if not existing:
                    print("  No external theme files yet — dumping natives so you have files to edit…")
                    try:
                        results = themes_mod.dump_builtin_themes()
                        written = sum(1 for v in results.values() if v == "written")
                        print(f"    ✓ wrote {written} theme files to {themes_mod.themes_dir()}")
                    except Exception as e:
                        print(f"    dump failed: {e}")
                        continue
            theme_source = new_src
            cfg.setdefault("ui", {})["theme_source"] = new_src
            save_config(cfg)
            print(f"  saved theme_source = {new_src}")
            themes_list = _refresh_list()
            continue
        if lower.startswith("open"):
            parts = raw.split(None, 1)
            if len(parts) < 2:
                print("  usage: open <name>")
                continue
            name = parts[1].strip()
            if themes_mod is None:
                print("  themes module unavailable.")
                continue
            p = themes_mod.themes_dir() / f"{name}.toml"
            print(f"  {p}  ({'exists' if p.exists() else 'missing — run dump first'})")
            continue
        if lower.startswith("preview"):
            parts = raw.split(None, 1)
            name_input = parts[1].strip() if len(parts) > 1 else ""
            resolved, ambig = resolve_selection(name_input or current, themes_list)
            if resolved is None:
                if ambig:
                    print(f"  '{name_input}' is ambiguous: {', '.join(ambig[:8])}")
                else:
                    print(f"  unknown theme: {name_input}")
                continue
            _run_theme_preview(cfg, resolved)
            continue

        # ----- Selection -----
        resolved, ambig = resolve_selection(raw, themes_list)
        if resolved is None:
            if ambig:
                print(f"  '{raw}' is ambiguous — matches:")
                for m in ambig[:10]:
                    print(f"    {m}")
                continue
            print(f"  no theme matches '{raw}' — try a number 1-{len(themes_list)} or a name")
            continue

        cfg.setdefault("ui", {})["theme"] = resolved
        save_config(cfg)
        current = resolved
        print(f"  saved theme = {resolved}")
        return


def _run_theme_preview(cfg, name: str) -> None:
    """5-second demo render of a named theme."""
    import time as _t
    from ui import make_ui as _make
    print(f"\n  previewing '{name}' for 5 seconds...")
    ui_cfg = cfg.get("ui", {})
    demo = _make(
        theme=name,
        mode_label="Preview",
        force_terminal=ui_cfg.get("force_terminal", False),
        color_system=ui_cfg.get("color_system", "auto"),
    )
    with demo:
        demo.set_total(100)
        demo.update(
            current_folder="/mnt/Expansion/rips/Aphex Twin/SAW 85-92",
            current_file="03 - Xtal.flac",
            file_index_in_folder=3,
            files_in_folder=12,
            file_size_bytes=4194304,
            file_codec="flac",
            file_format_detail="16-bit / 44.1 kHz",
            organising_to="/mnt/Expansion/Organised/High Quality/R&S Records/Aphex Twin - Selected Ambient Works 85-92 - 1992/03 - Xtal.flac",
        )
        demo.set_grabbing({
            "artist": "Aphex Twin", "album": "Selected Ambient Works 85-92",
            "label": "R&S Records", "year": "1992", "track_number": "03",
            "codec": "flac",
        })
        for i in range(5):
            demo.log(["imported", "duplicate", "info"][i % 3],
                     f"demo entry {i+1}")
        for i in range(50):
            demo.advance(imported=True, size_bytes=4194304)
            _t.sleep(0.1)


def cmd_do_everything(cfg, db) -> None:
    """
    'Do Everything For Me' pipeline:

        Rebuild Database  →  Fetch Metadata  →  Organise

    This is the maintenance loop you'd run on a library after dropping
    new rips in: it reindexes what's on disk, fills missing tags from
    external providers, then re-organises folders to match the now-
    improved metadata.

    Two modes:

      AUTOMATIC — uses saved last-used defaults for every prompt in
        every sub-command. Single confirmation at the start. Walks the
        whole pipeline without further input. The 'press enter × 7'
        workflow extended to the entire library-maintenance loop.

      MANUAL — for each step, asks 'do this step? Y/n'. If yes, runs
        that step with its normal interactive prompts. Skip a step
        with 'n' and the pipeline moves to the next. This is useful
        when you want to e.g. skip the slow MB fetch this time but
        still rebuild + organise.

    Each sub-command writes its own progress to the UI; we just chain
    them. Errors in one step don't stop the others — the user is told
    what failed and asked whether to continue.
    """
    if not _check_writable(cfg):
        return
    _enter_subscreen(cfg)
    print()
    print("  ┌─ Do Everything For Me ──────────────────────────────────┐")
    print("  │                                                          │")
    print("  │  Runs the full library-maintenance pipeline:             │")
    print("  │   1. Rebuild database     (refresh DB from disk)         │")
    print("  │   2. Fetch metadata       (fill missing tags from MB+...)│")
    print("  │   3. Organise             (re-sort folders by new tags)  │")
    print("  │                                                          │")
    print("  └──────────────────────────────────────────────────────────┘")
    print()
    print("  Modes:")
    print("    a — AUTOMATIC: use saved defaults, no interruptions")
    print("    m — MANUAL: ask about each step + its config separately")
    print("    q — cancel")
    try:
        mode_raw = input("  > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if mode_raw in ("q", "cancel", ""):
        return
    if mode_raw in ("a", "auto", "automatic"):
        mode = "automatic"
    elif mode_raw in ("m", "manual"):
        mode = "manual"
    else:
        print(f"  unknown mode '{mode_raw}'; aborting.")
        return

    save_last_used(cfg, "do_everything", "mode", mode)

    # ----- helper: ask-or-default ---------------------------------------
    # In automatic mode every yes/no defaults to True (run the step) and
    # we don't prompt. In manual mode we prompt with the last-used answer
    # (or True) as default.
    def step_yes(question: str, key: str) -> bool:
        if mode == "automatic":
            return True
        return ask_yn_with_last_used(
            cfg, "do_everything", key, question, default=True,
        )

    # ----- Step 1: Rebuild database -------------------------------------
    if step_yes("Run step 1 — rebuild database?", "do_rebuild"):
        print()
        print("  ┌─ Step 1: rebuild database ──────────────────────────┐")
        try:
            if mode == "automatic":
                # Drive the rebuild silently with defaults. cmd_reindex
                # would prompt for the path; we hand it the configured
                # destination_root directly via a non-interactive path.
                _do_everything_rebuild_silent(cfg, db)
            else:
                cmd_reindex(cfg, db)
        except KeyboardInterrupt:
            print()
            print("  ✗ rebuild interrupted")
            if not _ask_yn("Continue to next step (fetch metadata)?", default=False):
                return
        except Exception as e:
            print()
            print(f"  ✗ rebuild failed: {e}")
            _log_exc("do_everything: rebuild step failed")
            if not _ask_yn("Continue to next step despite the error?", default=False):
                return
    else:
        print("  skipped step 1 (rebuild)")

    # ----- Step 2: Fetch metadata ---------------------------------------
    if step_yes("Run step 2 — fetch metadata from providers?", "do_fetch"):
        print()
        print("  ┌─ Step 2: fetch metadata ────────────────────────────┐")
        try:
            if mode == "automatic":
                _do_everything_fetch_silent(cfg, db)
            else:
                cmd_fetch_metadata(cfg, db)
        except KeyboardInterrupt:
            print()
            print("  ✗ fetch interrupted")
            if not _ask_yn("Continue to next step (organise)?", default=False):
                return
        except Exception as e:
            print()
            print(f"  ✗ fetch failed: {e}")
            _log_exc("do_everything: fetch step failed")
            if not _ask_yn("Continue to next step despite the error?", default=False):
                return
    else:
        print("  skipped step 2 (fetch)")

    # ----- Step 3: Organise ---------------------------------------------
    if step_yes("Run step 3 — organise files into canonical folders?", "do_organise"):
        print()
        print("  ┌─ Step 3: organise ──────────────────────────────────┐")
        try:
            if mode == "automatic":
                # Automatic organise = real run, no preview. Manual goes
                # through cmd_organise's normal preview/go flow.
                _do_everything_organise_silent(cfg, db)
            else:
                cmd_organise(cfg, db)
        except KeyboardInterrupt:
            print()
            print("  ✗ organise interrupted")
        except Exception as e:
            print()
            print(f"  ✗ organise failed: {e}")
            _log_exc("do_everything: organise step failed")
    else:
        print("  skipped step 3 (organise)")

    print()
    print("  ✓ pipeline complete.")


# ---------------------------------------------------------------------------
# AUTOMATIC-MODE SILENT DRIVERS
# ---------------------------------------------------------------------------
# Each of these calls into the relevant subsystem with NO interactive
# prompts, using saved last-used choices + sensible defaults. They're
# only invoked from cmd_do_everything's automatic branch.

def _do_everything_rebuild_silent(cfg, db) -> None:
    """Rebuild DB from disk with no prompts. Uses paths.destination_root."""
    from indexer import index_tree
    from ui import make_ui

    dest = cfg.get("paths", {}).get("destination_root", "").strip()
    if not dest:
        print("  ✗ paths.destination_root is empty — cannot rebuild silently")
        return

    ui_cfg = cfg.get("ui", {})
    ui = make_ui(
        theme=ui_cfg.get("theme", "rainbowdash"),
        mode_label="Rebuild",
        use_rich=ui_cfg.get("use_rich", True),
        refresh_per_second=ui_cfg.get("refresh_per_second", 20),
        force_terminal=ui_cfg.get("force_terminal", False),
        color_system=ui_cfg.get("color_system", "auto"),
    )
    with ui:
        index_tree(dest, cfg=cfg, db=db, ui=ui, force=False)


def _do_everything_fetch_silent(cfg, db) -> None:
    """Fetch metadata with saved last-used choices, no prompts."""
    from metadata_lookup import fill_missing_metadata, FILLABLE_TAGS
    from metadata_providers import make_provider
    from ui import make_ui

    provider_ids = load_last_used(
        cfg, "fetch_metadata", "providers",
        fallback=["musicbrainz", "deezer"],
    )
    providers = []
    for pid in provider_ids:
        prov = make_provider(pid)
        if prov is None:
            continue
        try:
            ok = prov.configure(cfg, lambda **kw: cfg_ask(cfg, **kw))
        except Exception:
            continue
        if ok:
            providers.append(prov)
    if not providers:
        print("  ✗ no providers available")
        return

    # Map the saved tier choice to the same column lists as cmd_fetch_metadata.
    # New semantics (v0.18): 1=QUICK, 2=THOROUGH, 3=FULL (default), 4=CUSTOM.
    tier_choice = load_last_used(cfg, "fetch_metadata", "tier", fallback="3")
    quick_cols = ["artist", "album", "title", "year", "genre"]
    thorough_cols = [
        "artist", "albumartist", "album", "title", "year",
        "label", "catalog_number", "genre",
        "country", "barcode",
        "musicbrainz_albumid", "musicbrainz_artistid",
    ]
    # FULL: ALL columns the DB has that come from providers (not local-only
    # fields like path/mtime/status). Built dynamically from the schema so
    # it auto-expands when we add archival columns.
    try:
        all_cols = [r["name"] for r in db.conn.execute("PRAGMA table_info(files)")]
        BLOCK = {
            "path", "mtime", "size_bytes", "content_hash", "source_root",
            "organised_path", "status", "imported_at",
            "broken_reason", "is_high_quality",
            "transcode_checked", "transcode_suspected", "transcode_cutoff_hz",
            "transcode_confidence", "transcode_notes",
            "first_seen", "last_seen",
        }
        full_cols = [c for c in all_cols if c not in BLOCK]
    except Exception:
        full_cols = [c for c, _ in FILLABLE_TAGS]

    if tier_choice == "1":
        cols = quick_cols
    elif tier_choice == "2":
        cols = thorough_cols
    elif tier_choice == "4":
        cols = load_last_used(cfg, "fetch_metadata", "custom_cols",
                              fallback=full_cols)
    else:  # default "3" = FULL
        cols = full_cols

    fetch_covers = load_last_used(cfg, "fetch_metadata", "fetch_covers", fallback=True)
    # 'replace_mode' takes precedence over old 'only_missing' bool
    replace_mode = load_last_used(cfg, "fetch_metadata", "replace_mode", fallback=None)
    if replace_mode is not None:
        only_missing = (str(replace_mode) == "1")
    else:
        only_missing = load_last_used(cfg, "fetch_metadata", "only_missing", fallback=True)
    resolve_bpm  = load_last_used(cfg, "fetch_metadata", "resolve_bpm", fallback=True)

    ui_cfg = cfg.get("ui", {})
    ui = make_ui(
        theme=ui_cfg.get("theme", "rainbowdash"),
        mode_label="Populate",
        use_rich=ui_cfg.get("use_rich", True),
        refresh_per_second=ui_cfg.get("refresh_per_second", 20),
        force_terminal=ui_cfg.get("force_terminal", False),
        color_system=ui_cfg.get("color_system", "auto"),
    )
    write_to_files       = load_last_used(cfg, "fetch_metadata", "write_to_files", fallback=True)
    touch_verified_rips  = load_last_used(cfg, "fetch_metadata", "touch_verified_rips", fallback=False)
    write_nfo            = load_last_used(cfg, "fetch_metadata", "write_nfo", fallback=True)
    commit_per_file      = load_last_used(cfg, "fetch_metadata", "commit_per_file", fallback=False)

    # Confidence-gating params (same logic as cmd_fetch_metadata above).
    fm_cfg = cfg.get("fetch_metadata", {}) or {}
    min_conf = float(fm_cfg.get("min_confidence_score", 80.0))
    amb_margin = float(fm_cfg.get("ambiguity_margin", 5.0))
    pp_thresholds = {str(k): float(v) for k, v in
                     (fm_cfg.get("thresholds", {}) or {}).items()}

    with ui:
        fill_missing_metadata(
            db,
            providers=providers,
            target_columns=cols,
            only_missing=only_missing,
            fetch_covers=fetch_covers,
            resolve_bpm=resolve_bpm,
            write_to_files=write_to_files,
            touch_verified_rips=touch_verified_rips,
            commit_per_file=commit_per_file,
            resume=True,                   # always resume in do-everything
            db_path=str(db.path),
            min_confidence_score=min_conf,
            ambiguity_margin=amb_margin,
            per_provider_thresholds=pp_thresholds,
            ui=ui,
        )

    if write_nfo:
        try:
            from nfo_writer import write_album_nfos_for_db
            write_album_nfos_for_db(
                db,
                style=cfg.get("nfo", {}).get("style", "nfo"),
                overwrite=bool(cfg.get("nfo", {}).get("overwrite", False)),
            )
        except ImportError as e:
            _log(f"nfo_writer unavailable in silent driver: {e}")


def _do_everything_organise_silent(cfg, db) -> None:
    """Run organise_in_place as a real (non-dry) run."""
    from importer import organise_in_place
    from ui import make_ui

    ui_cfg = cfg.get("ui", {})
    ui = make_ui(
        theme=ui_cfg.get("theme", "rainbowdash"),
        mode_label="Organise",
        use_rich=ui_cfg.get("use_rich", True),
        refresh_per_second=ui_cfg.get("refresh_per_second", 20),
        force_terminal=ui_cfg.get("force_terminal", False),
        color_system=ui_cfg.get("color_system", "auto"),
    )
    with ui:
        organise_in_place(db, cfg=cfg, ui=ui, dry_run=False)


def cmd_rip_audit(cfg, db) -> None:
    """
    Audio-based confirmation of suspected lossy transcodes, via the
    Vamp lossy-encoding-detector plugin.

    Gated to files already marked transcode_suspected=1 by fake_flac.py
    (option 5 Verify FLACs). Running this on the whole library is
    impractical — full mode is ~80 sec/file (190 days for 207k files);
    quick mode is ~3 sec/file (7 days). Running only on prior suspects
    drops it to hours.

    Requires sonic-annotator + the vamp-lossy-encoding-detector plugin
    to be installed. Prints install hints if either is missing.
    """
    _enter_subscreen(cfg)
    try:
        import rip_audio
    except ImportError as e:
        print(f"  rip_audio module unavailable: {e}")
        return

    sa = rip_audio.find_sonic_annotator()
    if not sa:
        print()
        print("  sonic-annotator is not installed.")
        print(rip_audio.install_hint())
        return

    if not rip_audio.is_plugin_installed(quick=True):
        print()
        print("  sonic-annotator is installed, but the lossy-encoding-detector")
        print("  plugin isn't loaded. Install instructions:")
        print(rip_audio.install_hint())
        return

    # Count how many files are flagged as suspects (so user can decide)
    try:
        suspect_count = db.conn.execute(
            "SELECT COUNT(*) FROM files WHERE transcode_suspected = 1"
        ).fetchone()[0]
    except Exception:
        suspect_count = 0

    if suspect_count == 0:
        print()
        print("  No files marked transcode_suspected=1 in the DB.")
        print("  Run option 5 (Verify FLACs) first to flag candidates,")
        print("  then come back here to confirm them with audio analysis.")
        return

    print()
    print(f"  {suspect_count:,} files flagged as suspected transcodes by spectral check.")
    print()
    print("  Mode:")
    print("    q — QUICK (1-second sample, ~3 sec/file)")
    print("    f — FULL  (entire file, ~80 sec/file, more accurate)")
    print(f"    [default = q]")
    try:
        m = input("  > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if m == "":
        m = "q"
    if m not in ("q", "f", "quick", "full"):
        print("  invalid mode; aborting.")
        return
    quick = (m in ("q", "quick"))

    # Estimate runtime so the user can ctrl-C before committing
    sec_per = 3 if quick else 80
    eta_sec = suspect_count * sec_per
    eta_min = eta_sec / 60
    eta_h = eta_min / 60
    eta_str = (f"{eta_sec:.0f} sec" if eta_sec < 120 else
               f"{eta_min:.1f} min" if eta_min < 120 else
               f"{eta_h:.1f} hours")
    print(f"  Estimated runtime: {eta_str}")
    print()

    # Cap option for sanity-testing
    try:
        cap_raw = input("  Limit to first N files (empty = all): ").strip()
        limit = int(cap_raw) if cap_raw else 0
    except (ValueError, EOFError, KeyboardInterrupt):
        return

    if not _ask_yn("Proceed?", default=True):
        return

    print()
    log_entries = []
    def log(level, msg):
        log_entries.append((level, msg))
        print(f"  [{level}] {msg}")

    stats = rip_audio.run_on_suspects(db, quick=quick, limit=limit, log_cb=log)

    print()
    print(f"  Done. {stats['checked']:,} files checked.")
    print(f"    Confirmed lossy:    {stats['confirmed_lossy']:,}")
    print(f"    Confirmed original: {stats['confirmed_lossless']:,}")
    print(f"    Errors:             {stats['errors']:,}")
    print(f"    Skipped (missing):  {stats['skipped']:,}")
    print()
    print("  Verdicts stored in `transcode_notes` column as 'vamp:lossy:0.99'")
    print("  or 'vamp:original:0.99'. Use option b (Browse) to see them.")


def cmd_quarantine(cfg, db) -> None:
    """
    Move files confirmed as fake-FLAC / lossy-transcode out of the
    normal library tree into a Quarantine folder. The DB row is kept
    so you can review later; the file's `organised_path` and `status`
    are updated.

    What this is NOT: a re-encoder. Re-encoding a fake-FLAC to MP3
    won't recover the original lossy file (the bytes are gone). Best
    we can do is move them aside so they don't pollute your lossless
    library, then YOU decide what to do with them (delete, replace
    from a better source, etc.).
    """
    if not _check_writable(cfg):
        return
    _enter_subscreen(cfg)

    dest_root = cfg.get("paths", {}).get("destination_root", "").strip()
    if not dest_root:
        print("  No destination_root configured.")
        return
    quarantine_dir = Path(dest_root) / cfg.get(
        "paths", {}
    ).get("quarantine_folder", "Quarantine — Confirmed Lossy")

    # Files we'd quarantine: vamp confirmed lossy AND not already in there.
    cursor = db.conn.execute(
        "SELECT path, artist, album, title, codec, transcode_notes "
        "FROM files "
        "WHERE transcode_suspected = 1 "
        "  AND (transcode_notes LIKE 'vamp:lossy:%' "
        "    OR (transcode_confidence IS NOT NULL AND transcode_confidence >= 0.9)) "
        "  AND status != 'quarantined' "
        "  AND status != 'broken'"
    )
    candidates = [dict(r) for r in cursor]
    if not candidates:
        print()
        print("  No confirmed-lossy files to quarantine.")
        print("  Run option 5 (Verify FLACs) to flag suspects, then")
        print("  the Vamp audit when prompted to confirm them.")
        return

    print()
    print(f"  Found {len(candidates):,} files confirmed as lossy transcodes.")
    print(f"  Quarantine folder: {quarantine_dir}")
    print()
    print("  Preview (first 10):")
    for r in candidates[:10]:
        artist = (r.get("artist") or "?")[:25]
        album = (r.get("album") or "?")[:30]
        title = (r.get("title") or "?")[:25]
        notes = (r.get("transcode_notes") or "")[:30]
        print(f"    {artist} / {album} / {title}   [{notes}]")
    if len(candidates) > 10:
        print(f"    ... and {len(candidates) - 10:,} more")
    print()
    if not _ask_yn("Move these files to quarantine?", default=False):
        return

    quarantine_dir.mkdir(parents=True, exist_ok=True)
    moved = 0
    errors = 0
    for r in candidates:
        src = Path(r["path"])
        if not src.exists():
            errors += 1
            continue
        # Preserve the artist/album folder structure under quarantine
        try:
            rel = src.relative_to(dest_root)
        except ValueError:
            rel = Path(src.name)
        dst = quarantine_dir / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
            db.conn.execute(
                "UPDATE files SET path=?, status='quarantined', "
                "organised_path=? WHERE path=?",
                (str(dst), str(dst), str(src)),
            )
            db.conn.commit()
            moved += 1
        except Exception as e:
            print(f"  ! could not move {src.name}: {e}")
            errors += 1
    print()
    print(f"  Moved {moved:,} file(s) to quarantine. Errors: {errors:,}")
    print()
    print("  These rows are now marked status='quarantined' in the DB.")
    print("  They no longer appear in your organised tree, but you can")
    print("  still see them via the Browse command (option b).")


def cmd_transcode_suspects(cfg, db) -> None:
    """
    OPTIONAL re-encoder for confirmed fake-FLAC files. Re-encodes each
    file to a target lossy format (MP3 V0 by default) and updates the DB.

    WHY THIS HAS LOUD WARNINGS:

    If a "FLAC" was already transcoded from an MP3, the file already
    contains lossy-decoded audio. Re-encoding it again gives you a
    SECOND-GENERATION lossy file — strictly worse than the original
    MP3 source you can't recover. The honest use case for this command
    is: "I know these aren't real lossless and I'd rather save the
    disk space than keep the fake-FLAC fiction."

    Default behaviour: DRY RUN. The user explicitly opts into actual
    re-encoding. We also default-on `keep_originals` so nothing is
    deleted until the user verifies the output.

    Requires `ffmpeg` on PATH.
    """
    if not _check_writable(cfg):
        return
    _enter_subscreen(cfg)

    if not shutil.which("ffmpeg"):
        print()
        print("  ✗ ffmpeg not found on PATH. Install it first:")
        try:
            from platform_detect import detect_os, install_command
            os_info = detect_os()
            cmd, _ = install_command(os_info, "ffmpeg")
            if cmd:
                print(f"    {cmd}")
        except Exception:
            print("    (consult your distro's package manager)")
        return

    # Find candidates: vamp-confirmed-lossy or high-confidence spectral suspects
    cursor = db.conn.execute(
        "SELECT path, artist, album, title, codec, bitrate, transcode_notes, "
        "       transcode_confidence "
        "FROM files "
        "WHERE transcode_suspected = 1 "
        "  AND status != 'quarantined' "
        "  AND status != 'broken' "
        "  AND (transcode_notes LIKE 'vamp:lossy:%' "
        "    OR transcode_confidence >= 0.9)"
    )
    candidates = [dict(r) for r in cursor]
    if not candidates:
        print()
        print("  No confirmed-lossy candidates found.")
        print("  Run option 5 (Verify rips) → Vamp confirm first to flag them.")
        return

    # The Big Warning Sign
    print()
    print("  " + "=" * 64)
    print("  ⚠  RE-ENCODE WARNING — READ THIS")
    print("  " + "=" * 64)
    print()
    print("  Re-encoding a fake-FLAC (= a FLAC that was already transcoded")
    print("  from lossy) to MP3 produces a SECOND-GENERATION lossy file.")
    print("  The original lossy source you can't recover is gone forever;")
    print("  what's left in the FLAC IS the lossy-decoded waveform. Going")
    print("  through MP3 again makes it strictly worse.")
    print()
    print(f"  Candidates found: {len(candidates):,}")
    print()
    print("  Use this only if you understand the tradeoff. The honest case")
    print("  is: 'I'd rather save disk space than keep the fake-FLAC fiction.'")
    print()
    print("  " + "=" * 64)
    print()
    if not _ask_yn("Do you understand the above and want to continue?",
                    default=False):
        return

    print()
    print("  Target encoding:")
    print("    1. MP3 V0   (~245 kbps avg, smallest decent quality)")
    print("    2. MP3 320  (~320 kbps, larger files)")
    print("    3. Opus 192 (~192 kbps, modern codec, smallest at quality)")
    print("    4. Opus 128 (~128 kbps, smaller still)")
    print("    q. cancel")
    try:
        choice = input("  > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if choice in ("q", ""):
        return
    encode_specs = {
        "1": ("mp3", ["-c:a", "libmp3lame", "-q:a", "0"]),
        "2": ("mp3", ["-c:a", "libmp3lame", "-b:a", "320k"]),
        "3": ("opus", ["-c:a", "libopus", "-b:a", "192k"]),
        "4": ("opus", ["-c:a", "libopus", "-b:a", "128k"]),
    }
    spec = encode_specs.get(choice)
    if spec is None:
        print("  invalid choice; aborting.")
        return
    target_ext, ffmpeg_args = spec

    keep_originals = _ask_yn(
        "Keep the original FLAC files (.flac.bak) alongside the new ones?",
        default=True,
    )
    dry_run = _ask_yn(
        "DRY RUN first? (print what would happen, don't actually re-encode)",
        default=True,
    )

    # Estimate runtime — ffmpeg averages 5-15 sec per track depending on
    # length and codec. Use the candidates list to estimate.
    est_sec = len(candidates) * 10
    est_min = est_sec / 60
    print()
    print(f"  Estimated runtime: ~{est_min:.0f} min for {len(candidates):,} files")
    print()
    if not _ask_yn("Proceed?", default=False):
        return

    success, failed, skipped = 0, 0, 0
    for i, row in enumerate(candidates, 1):
        src = Path(row["path"])
        if not src.exists():
            skipped += 1
            print(f"  [skip] {i}/{len(candidates)}: {src.name} (file missing)")
            continue
        dst = src.with_suffix(f".{target_ext}")
        print(f"  [{i}/{len(candidates)}] {src.name} → {dst.name}")
        if dry_run:
            success += 1
            continue
        try:
            r = subprocess.run(
                ["ffmpeg", "-y", "-i", str(src), *ffmpeg_args,
                 "-loglevel", "error", str(dst)],
                capture_output=True, text=True, timeout=300,
            )
            if r.returncode != 0:
                failed += 1
                print(f"     ✗ ffmpeg: {r.stderr.strip()[:100]}")
                if dst.exists():
                    dst.unlink()
                continue
            if keep_originals:
                src.rename(src.with_suffix(".flac.bak"))
            else:
                src.unlink()
            # Update DB: point row to new file, mark status
            db.conn.execute(
                "UPDATE files SET path=?, codec=?, status='transcoded', "
                "                  transcode_notes=COALESCE(transcode_notes,'') "
                "                                   || ' | re-encoded ' || ? "
                "WHERE path=?",
                (str(dst), target_ext, target_ext, row["path"]),
            )
            db.conn.commit()
            success += 1
        except subprocess.TimeoutExpired:
            failed += 1
            print(f"     ✗ ffmpeg timed out")
        except Exception as e:
            failed += 1
            print(f"     ✗ {type(e).__name__}: {e}")

    print()
    if dry_run:
        print(f"  DRY RUN complete. Would have re-encoded {success:,} files.")
        print(f"  Re-run with dry-run = N to actually do it.")
    else:
        print(f"  Done. success={success:,}  failed={failed:,}  skipped={skipped:,}")


def cmd_pony(cfg, db) -> None:
    """
    Print the current theme's pony art (if it has one), or pick one
    to display. Useful as a quick visual check of decoration after
    editing a theme file.

    Usage at the prompt:
      pony              → print the active theme's pony at full size
      pony <name>       → print a specific pony (rainbow, twilight,
                          pinkie, rarity, applejack, fluttershy)
      pony <name> half  → print just the top half
      pony list         → list all available ponies
    """
    _enter_subscreen(cfg)
    try:
        import decoration
    except ImportError:
        print("  decoration module unavailable.")
        return
    try:
        import themes as themes_mod
    except ImportError:
        themes_mod = None

    print()
    print(f"  Available ponies: {', '.join(decoration.list_available_ponies())}")
    print(f"  + 'nyancat' for the static watermark")
    print()
    print("  Type a name to print, or 'q' to return.")
    print("  Add ' half' or ' tiny' after the name for smaller versions.")
    print()
    # Try the theme's decoration as the suggested default
    theme_name = cfg.get("ui", {}).get("theme", "cyber")
    suggested = ""
    if themes_mod is not None:
        try:
            dec = themes_mod.get_decoration(theme_name)
            if dec.get("pony"):
                suggested = dec["pony"]
                print(f"  Active theme '{theme_name}' suggests: {suggested}")
        except Exception:
            pass
    print()
    while True:
        try:
            raw = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not raw or raw.lower() in ("q", "quit", "back"):
            return
        if raw.lower() == "list":
            print(f"  {', '.join(decoration.list_available_ponies())}")
            continue
        parts = raw.split()
        name = parts[0]
        size = parts[1] if len(parts) > 1 else "full"
        if size not in ("full", "half", "tiny"):
            print(f"  size '{size}' not recognised; use full/half/tiny")
            continue
        if name == "nyancat":
            # Render the markup version through Rich for the watermark
            try:
                from rich.console import Console
                Console().print(decoration.NYANCAT_WATERMARK)
            except Exception:
                print(decoration.NYANCAT_WATERMARK)
            continue
        art = decoration.render_pony_panel(name, size=size)
        if art is None:
            print(f"  no pony art for {name!r}. try one of: "
                  f"{', '.join(decoration.list_available_ponies())}")
            continue
        # Pony art has raw ANSI escapes; print() handles it fine on any
        # 256-colour terminal.
        print()
        print(art)
        print()


def cmd_analyze_rip(cfg, db) -> None:
    """
    Deep audio analysis of one or more files. Hidden command — reachable
    by typing 'analyze' (or 'analyse', 'inspect', etc.) at the prompt.

    Detects (from the audio bits themselves, not log files):
      • Lossy-source-in-FLAC (fake FLAC) via spectral cutoff
      • Fake hi-res (24/96 claim but actually a 16-bit upsample) via
        bottom-bit silence + spectral cutoff
      • Genuine CD spectrum (positive confirmation)

    Honest limits — we CAN'T detect from the audio alone:
      • Which ripper was used (EAC vs XLD vs dBpoweramp)
      • Whether AccurateRip would match a reference checksum
      • Vinyl vs CD source reliably (too many false positives)

    Slow: ~5-30 seconds per file. Meant for spot-checking suspicious
    rips, not batch operations.
    """
    _enter_subscreen(cfg)
    try:
        from rip_analyze import analyze_audio_origin, format_analysis, _have_ffmpeg
    except ImportError:
        print("  rip_analyze module unavailable.")
        return

    if not _have_ffmpeg():
        print()
        print("  ⚠  ffmpeg is not installed.")
        print()
        print("  Deep audio analysis needs ffmpeg to decode samples for")
        print("  spectral and bit-level inspection. Without it, we can only")
        print("  read file headers — which doesn't catch fake-FLAC or fake-")
        print("  hi-res. Install with:")
        print("       sudo pacman -S ffmpeg")
        print()
        cont = input("  continue with header-only analysis? [y/N] ").strip().lower()
        if cont not in ("y", "yes"):
            return

    print()
    print("  Deep audio analysis")
    print("  " + "─" * 50)
    print()
    print("  Enter a file path, a folder path, or 'q' to quit.")
    print("  Folder: analyses up to 10 random audio files inside.")
    print("  Tip: drag-and-drop a file/folder into your terminal pastes its path.")
    print()

    AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus",
                   ".wav", ".aiff", ".aif", ".wv", ".ape"}

    while True:
        try:
            target = input("  path > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not target or target.lower() in ("q", "quit", "exit"):
            return

        # Strip wrapping quotes that terminal drag-and-drop adds
        target = target.strip("'\"")

        p = Path(target).expanduser()
        if not p.exists():
            print(f"  ✗ doesn't exist: {p}")
            continue

        # Build the list of files to analyse
        if p.is_file():
            files = [p]
        elif p.is_dir():
            # Find audio files, sample up to 10 (random for variety)
            audio_files = [
                f for f in p.rglob("*")
                if f.is_file() and f.suffix.lower() in AUDIO_EXTS
            ]
            if not audio_files:
                print(f"  ✗ no audio files found in {p}")
                continue
            import random
            sample_count = min(10, len(audio_files))
            files = random.sample(audio_files, sample_count)
            print(f"  found {len(audio_files):,} audio files; analysing "
                  f"{sample_count} random sample(s)...")
        else:
            print(f"  ✗ not a file or directory: {p}")
            continue

        print()
        flagged: list = []
        for i, f in enumerate(files, 1):
            print(f"  [{i}/{len(files)}] {f.name}")
            try:
                result = analyze_audio_origin(f, deep=True, timeout_s=45)
                print(format_analysis(result, verbose=True))
                if (result.likely_fake_hires or
                    result.likely_lossy_source):
                    flagged.append(result)
            except KeyboardInterrupt:
                print("\n  (interrupted)")
                break
            except Exception as e:
                print(f"    error analysing: {e}")
            print()

        if flagged:
            print(f"  ⚠  {len(flagged)} file(s) flagged:")
            for r in flagged:
                tags = []
                if r.likely_lossy_source: tags.append("LOSSY")
                if r.likely_fake_hires:   tags.append("FAKE-HIRES")
                print(f"      [{' / '.join(tags)}] {Path(r.path).name}")
            print()


def cmd_guide(cfg, db) -> None:
    """
    Man-page-style in-program guide. Multi-section reference document
    covering every command, config key, file format, and design choice.

    Layout follows traditional man-page sections:
      NAME, SYNOPSIS, DESCRIPTION, COMMANDS, FILES, CONFIGURATION,
      ENVIRONMENT, BUGS, SEE ALSO.

    Long output by design — pages through the terminal pager if one
    is available (less / more), falls back to plain print otherwise.
    """
    _enter_subscreen(cfg)
    try:
        from menu_items import MENU_ITEMS
        from icons import icon
    except Exception as e:
        print(f"  guide unavailable: {e}")
        return

    import io
    out = io.StringIO()
    p = lambda *args: print(*args, file=out)

    # ----- Header / NAME / SYNOPSIS ------------------------------
    p()
    p("MUSIC-ORGANISER(1)             User Commands             MUSIC-ORGANISER(1)")
    p()
    p("NAME")
    p("       music-organiser — content-aware music library importer, tagger,")
    p("       and re-organiser with provenance-respecting safeguards")
    p()
    p("SYNOPSIS")
    p("       python organiser.py")
    p("           Interactive menu. The only supported invocation.")
    p()

    # ----- DESCRIPTION -------------------------------------------
    p("DESCRIPTION")
    p("       music-organiser walks one or more SOURCE trees of audio files,")
    p("       imports them into a structured DESTINATION tree, populates an")
    p("       SQLite database with their metadata, and lets you fetch missing")
    p("       tags from online providers (MusicBrainz, Discogs, Deezer,")
    p("       Bandcamp, iTunes) with a multi-strategy fallback chain.")
    p()
    p("       The destination layout is:")
    p("           {destination_root}/")
    p("               High Quality/{label_or_artist}/{artist} - {album} - {year}/")
    p("                   01 - {title}.flac")
    p("                   cover.jpg")
    p("                   {album}.nfo")
    p("               Shit Quality/...   (lossy formats)")
    p("               Broken/...         (unreadable / failed)")
    p("               orphaned bonus files/...  (rescued lone tracks)")
    p()
    p("       Files in EAC/XLD-logged folders are never tag-rewritten in place")
    p("       unless touch_verified_rips is enabled — preserving rip checksums.")
    p()

    # ----- TYPICAL WORKFLOW --------------------------------------
    p("TYPICAL WORKFLOW")
    p("       1 -> 3 -> 7 -> 2")
    p("       (Import sources -> Rebuild DB -> Fetch tags -> Re-organise)")
    p()
    p("       For a fresh library:")
    p("         1. Run setup (s) to point at your source roots and dest path")
    p("         2. Run 1 (Import) to copy/move audio into the dest tree")
    p("         3. Run 3 (Rebuild) only if the DB seems out of sync with disk")
    p("         4. Run 7 (Fetch) to fill missing metadata from online sources")
    p("         5. Run 2 (Organise) to re-shuffle files based on freshly")
    p("            fetched tags. Often a no-op after step 4 wrote tags in")
    p("            place; only re-runs when actual moves are needed.")
    p()

    # ----- COMMANDS ----------------------------------------------
    p("COMMANDS")
    p()
    p("       The main menu is a single-character dispatcher with aliases. Every")
    p("       option accepts its number/letter, its full name, a unique prefix,")
    p("       or any registered alias. Type '<key>?' (e.g. '7?') for that")
    p("       option's detailed help screen.")
    p()
    groups = {"core": [], "audit": [], "tool": [], "config": []}
    for it in MENU_ITEMS:
        groups.setdefault(it.kind, []).append(it)
    group_titles = {
        "core":   "PIPELINE COMMANDS",
        "audit":  "QUALITY & VERIFICATION COMMANDS",
        "tool":   "INSPECTION & UTILITY COMMANDS",
        "config": "CONFIG & APPEARANCE COMMANDS",
    }
    for kind in ("core", "audit", "tool", "config"):
        items = groups.get(kind, [])
        if not items:
            continue
        p(f"   {group_titles[kind]}")
        for it in items:
            display_key = it.key if it.key else "·"
            p(f"       {display_key:<3} {it.label}")
            p(f"           {it.short_hint}")
            p()

    # ----- HIDDEN COMMANDS ---------------------------------------
    p("   HIDDEN COMMANDS")
    p("       analyze    Deep audio inspection. Header parsing + bottom-bit")
    p("                  silence check for fake hi-res + ripper-software ID")
    p("                  (vendor tag + proprietary tag fingerprints like")
    p("                  ACCURATERIPRESULT, XLD_ESTIMATED_DURATION).")
    p("       pony       MLP CLI viewer. Six characters available.")
    p("       transcode  Hidden alias for re-encoding (designed to be invoked")
    p("                  rarely; not on the main menu by design).")
    p()

    # ----- FILES -------------------------------------------------
    p("FILES")
    p()
    p("       /etc/music-organiser/config.toml")
    p("           System-wide defaults (rarely used).")
    p()
    p("       ~/.config/music-organiser/config.toml")
    p("           User config. Created on first run via the setup wizard.")
    p("           All keys are documented in config.default.toml.")
    p()
    p("       ~/.local/share/music-organiser/library.db")
    p("           SQLite database. WAL journal, ~128 columns, one row per")
    p("           audio file. Use option 8 (SQL) to query interactively.")
    p()
    p("       ~/.local/share/music-organiser/checkpoint.json")
    p("           Last-run state. Lets imports resume after Ctrl-C / power")
    p("           loss. Cleared on successful completion.")
    p()
    p("       ~/.local/share/music-organiser/lockfile")
    p("           PID-bearing lockfile. Reader/writer modes — multiple")
    p("           read-only instances are allowed; only one writer at a time.")
    p()
    p("       {destination_root}/High Quality/")
    p("           Lossless audio destination (FLAC, ALAC, WV, APE, DSF/DFF).")
    p()
    p("       {destination_root}/Shit Quality/")
    p("           Lossy audio destination (MP3, M4A AAC, OGG, OPUS, WMA).")
    p()
    p("       {destination_root}/Broken/")
    p("           Files that failed to read, parse, or copy. NEVER deleted")
    p("           automatically — manual triage required.")
    p()
    p("       {destination_root}/orphaned bonus files/")
    p("           Audio files rescued from non-music folders by the triage")
    p("           pass (paths.orphan_folder, default 'orphaned bonus files').")
    p()
    p("       {out_of_library_dest}/")
    p("           Folders classified as 'random crap' (no audio, 3+ mixed")
    p("           files like screenshots, installers, ebooks) get moved here.")
    p("           OUTSIDE destination_root so they don't get re-scanned.")
    p()

    # ----- CONFIGURATION -----------------------------------------
    p("CONFIGURATION")
    p()
    p("       Top-level sections, with key keys (see config.default.toml for")
    p("       full reference):")
    p()
    p("   [paths]")
    p("       sources                List of source root paths to import from")
    p("       destination_root       Where the organised tree lives")
    p("       broken_folder          Subdir name for unreadable files")
    p("       high_quality_folder    Subdir name for lossless")
    p("       low_quality_folder     Subdir name for lossy")
    p("       orphan_folder          Subdir name for rescued orphan audio")
    p("       out_of_library_dest    Random-crap destination (outside dest)")
    p("       database               SQLite DB path (~ expanded)")
    p()
    p("   [import]")
    p("       mode                   'copy' or 'move'. Use 'copy' unless")
    p("                              you're absolutely sure about the move.")
    p("       audio_extensions       Recognised audio types (lower-case,")
    p("                              leading dot, e.g. '.flac')")
    p("       extras_extensions      File types that travel with audio")
    p("                              (cover art, logs, cue, nfo, etc)")
    p("       junk_filenames         Files to silently skip (Thumbs.db etc)")
    p("       skip_hidden            Skip dotfiles and dotdirs (default true)")
    p("       follow_symlinks        Follow symlinks during walk (default")
    p("                              false — avoids loops)")
    p("       min_size_bytes         Files smaller than this are ignored")
    p("       skip_precount          Skip the initial count walk for ETA-less")
    p("                              streaming mode (faster start)")
    p("       triage_non_music       Enable random-crap / orphan-rescue pass")
    p("       random_crap_min_files  Junk file count threshold (default 3)")
    p("       orphan_bonus_max_audio Max audio files for orphan-rescue path")
    p()
    p("   [organise]")
    p("       mix_artist_diversity_threshold")
    p("                              0.0-1.0 — ratio of distinct artists in")
    p("                              an album above which it's treated as a")
    p("                              VA mix")
    p("       various_artists_tags   Strings recognised as 'VA' in tags")
    p("       mix_keywords           Words in album name that mark a mix")
    p("       delete_orphaned_extras Remove source folders left with only")
    p("                              extras (cover.jpg, log) after move")
    p()
    p("   [providers]")
    p("       providers.discogs.token     Discogs API token (FREE, required")
    p("                                   for Discogs to work at all). Get")
    p("                                   one at:")
    p("                                   https://www.discogs.com/settings/developers")
    p("       providers.musicbrainz.deep_harvest")
    p("                                   When true (default), every MB match")
    p("                                   triggers a second deep-detail fetch.")
    p("                                   Cost: ~1s per matched album.")
    p()
    p("   [ui]")
    p("       theme                  Theme name (see option t for list)")
    p("       nerd_font              'auto'/'on'/'off' — Nerd Font glyph use")
    p("       color_system           Rich color mode (auto/256/truecolor/none)")
    p("       progress_update_interval  How often the progress bar repaints")
    p("       animate_menu           Bring the menu in with a brief animation")
    p("       animate_menu_duration  Seconds the entry animation runs")
    p("       animate_while_input    Keep the title-bar marquee running while")
    p("                              the prompt waits for input")
    p()

    # ----- DATABASE LAYOUT ---------------------------------------
    p("DATABASE")
    p()
    p("       Single SQLite file with one table `files`. ~128 columns covering:")
    p("         - Filesystem identity: path, size, mtime, last_seen")
    p("         - Audio: codec, sample_rate, bit_depth, channels, bitrate,")
    p("                  duration, bpm")
    p("         - Tags: artist, album, title, track, disc, year, genre,")
    p("                 album_artist, composer, label, catalog_number")
    p("         - MB IDs: mb_release_id, mb_recording_id, mb_artist_id,")
    p("                   mb_releasegroup_id (Picard-compatible column names)")
    p("         - Discogs: discogs_release_id")
    p("         - Audit: status (ok/broken/duplicate/orphan), lossless,")
    p("                  fake_flac, suspect_lossy_source")
    p("         - Provenance: ripper (when detected), rip_log_present")
    p()
    p("       Write semantics: every metadata fetch upsert is a single")
    p("       INSERT...ON CONFLICT DO UPDATE statement against the open")
    p("       WAL-mode connection. SQLite auto-checkpoints the WAL")
    p("       periodically; explicit commits happen at the end of each")
    p("       bulk operation. Power-loss safety is provided by the WAL")
    p("       atomic append, not by per-file commits.")
    p()
    p("       Query interactively with option 8 (SQL prompt). Useful")
    p("       starting queries:")
    p("           SELECT COUNT(*) FROM files WHERE status='broken';")
    p("           SELECT artist, COUNT(*) FROM files GROUP BY artist")
    p("                   ORDER BY 2 DESC LIMIT 20;")
    p("           SELECT path FROM files WHERE bpm IS NULL")
    p("                   AND lossless = 1 LIMIT 50;")
    p()

    # ----- TAG-WRITING -------------------------------------------
    p("TAG WRITING")
    p()
    p("       The metadata fetcher writes resolved tags back into the audio")
    p("       file's metadata blocks (Vorbis comments for FLAC/OGG, ID3v2")
    p("       for MP3, MP4 atoms for M4A) via mutagen. This means tags")
    p("       travel with the file when you seed on Soulseek / move drives.")
    p()
    p("       Two safeguards apply:")
    p("         1. Files in folders containing an EAC or XLD log are")
    p("            NEVER touched in-place (the rip checksum would change).")
    p("            Override with config.import.touch_verified_rips = true.")
    p("         2. Files marked status='broken' are never written to.")
    p()

    # ----- METADATA FETCH STRATEGY -------------------------------
    p("METADATA FETCH STRATEGY")
    p()
    p("       The fetch loop (option 7) iterates each (artist, album) tuple")
    p("       and tries up to FIVE query strategies per album before giving")
    p("       up:")
    p()
    p("         1. Primary: (artist, album) as stored")
    p("         2. Cat-num stripped: bracketed catalogue prefixes removed")
    p("            e.g. '[SHADOW 082] Helicopter '97' -> 'Helicopter '97'")
    p("         3. VA + series + issue: when album matches a compilation")
    p("            series pattern (Dancemania Speed 6, Beatport Sound Pack")
    p("            #348, Hed Kandi, etc.), reroute as Various Artists")
    p("         4. VA + bare series (no issue): drop the volume number")
    p("         5. Per-recording sampling: pick 3 sample tracks from the")
    p("            album, query MB by recording title, vote on which release")
    p("            MBID appears most often.")
    p()
    p("       Layer above all of this: NEVER sends 'Unknown Artist',")
    p("       'Unknown Album', '<unknown>', 'N/A', etc. to providers.")
    p("       Path recovery tries to fill empty/placeholder values from")
    p("       folder names and filenames first, then from sibling tracks'")
    p("       tags. If still unknown, the file is skipped to avoid wasting")
    p("       rate-limit budget on guaranteed-misses.")
    p()
    p("       Providers, in default order:")
    p("         musicbrainz   (free, no auth, 1 req/sec)")
    p("         discogs       (free, requires read token, 60 req/min)")
    p("         deezer        (free, no auth, 50 req/sec)")
    p("         bandcamp      (free, scrapes search results)")
    p("         itunes        (free, no auth, ~20 req/min)")
    p()

    # ----- RESUME -----------------------------------------------
    p("RESUMING AN INTERRUPTED FETCH")
    p()
    p("       Fetch runs on big libraries can take hours. If the run is")
    p("       interrupted (Ctrl-C, freeze, power loss, OOM kill, USB drive")
    p("       disconnect), the next time you start option 7 you'll be")
    p("       prompted to resume from where you left off:")
    p()
    p("           Found an unfinished fetch run from earlier:")
    p("             Started: 2026-05-28 1:12 am")
    p("             Progress: 18,432 / 131,683 albums (14.0%)")
    p("             Files updated: 31,224  fields: 187,344  no-match: 4,108")
    p("             Providers: musicbrainz, discogs")
    p("             Last album: Smile.dk - Dancemania Speed 6")
    p()
    p("           Resume (skip already-processed albums)? [Y/n]")
    p()
    p("       Yes -> the next run skips every (artist, album) tuple already")
    p("              in the checkpoint. Saves potentially hours.")
    p("       No  -> the checkpoint is cleared and the run starts fresh.")
    p()
    p("       The checkpoint is at ~/.cache/music-organiser/fetch_checkpoint.json")
    p("       (or $XDG_CACHE_HOME/music-organiser/ if set). It's plain JSON")
    p("       so you can inspect or hand-edit if needed.")
    p()
    p("       The checkpoint is rewritten to disk every 10 albums OR every")
    p("       30 seconds, whichever comes first. Worst-case work loss on")
    p("       crash is therefore ~10 albums of re-querying, not the whole run.")
    p()
    p("       Resume only kicks in if the operation matches the previous run:")
    p("       same DB, same providers, same target_columns, same only_missing")
    p("       flag, same write_to_files flag. If any of those differ (e.g. you")
    p("       added a new provider), the checkpoint is considered stale and")
    p("       cleared automatically.")
    p()

    # ----- TRIAGE ------------------------------------------------
    p("FOLDER TRIAGE")
    p()
    p("       Opt-in via [import] triage_non_music = true. When enabled,")
    p("       a pre-pass classifies every subfolder of every source root:")
    p()
    p("         MUSIC           - has audio (>2 files OR clear music context)")
    p("                           Normal import path.")
    p("         ORPHAN_BONUS    - 1-2 audio files swimming in non-music junk")
    p("                           Audio rescued to paths.orphan_folder.")
    p("         RANDOM_CRAP     - no audio, 3+ mixed-type files")
    p("                           Whole folder moved to paths.out_of_library_dest.")
    p("         EMPTY/TRIVIAL   - empty or just 1-2 stray non-audio files")
    p("                           Left alone.")
    p()
    p("       Companion files (cover.jpg, log, cue, nfo, m3u) are recognised")
    p("       ONLY when audio is present. A folder with just three .jpg files")
    p("       and no audio counts as random crap, not as a 'cover-art folder'.")
    p()
    p("       Safety: triage never touches destination_root, never the source")
    p("       root itself, and skips during dry_run.")
    p()

    # ----- BROKEN-METADATA ROUTING -------------------------------
    p("BROKEN-METADATA ROUTING")
    p()
    p("       A track is 'broken' if it's missing artist, album, OR title.")
    p("       (Where 'missing' means empty, NULL, or a placeholder like")
    p("       'Unknown Artist' / 'Untitled' / 'N/A'.)")
    p()
    p("       Broken rows get status='broken' in the DB and are routed to")
    p("       the Broken/ folder by build_destination_path. Audio data is")
    p("       never lost — broken files are just sequestered until tags")
    p("       can be recovered.")
    p()
    p("       VA compilations are NOT special-cased. A real VA track has")
    p("       its own per-track artist (Smile.dk on Dancemania Speed 6),")
    p("       so it passes. A VA-tagged track where the per-track artist")
    p("       is also blank fails — correctly, because it's genuinely")
    p("       unidentifiable.")
    p()
    p("       The check fires automatically in three places:")
    p()
    p("         1. IMPORT time — every newly-extracted record. Marked")
    p("            broken with comment='metadata incomplete: artist missing'")
    p("            or similar, routes to Broken/ on the same import pass.")
    p()
    p("         2. FETCH time — after each per-file tag merge. Bidirectional:")
    p("            rows with required tags missing get marked broken; rows")
    p("            that WERE broken but now have all three get flipped back")
    p("            to status='ok'. Next organise relocates them.")
    p()
    p("         3. RETROACTIVE audit — menu option 4 ('Check database')")
    p("            then 'b'. One-shot scan that flips statuses to match")
    p("            the predicate. Dry-run preview before any write. Doesn't")
    p("            move files — that's the next organise pass's job.")
    p()
    p("       Statuses preserved (never overwritten):")
    p("         - 'duplicate'    (set by import dedup)")
    p("         - 'orphan'       (set by orphan-folder triage)")
    p("         - 'quarantine'   (set by fake-FLAC sweep)")
    p()
    p("       Un-breaking is conservative: only flips broken→ok when the")
    p("       comment doesn't mention copy errors, fake-flac, or quarantine.")
    p("       Can't undo a corrupt copy with a successful tag fetch.")
    p()

    # ----- PRETTY FILENAME REBUILDER -----------------------------
    p("PRETTY FILENAME REBUILDER")
    p()
    p("       Menu option 'f' (or aliases 'rename', 'tidy', 'fix-names').")
    p("       Renames files in place to a canonical pretty format.")
    p()
    p("       Format:")
    p("         ✰ [NN - Artist - Title - (freeform) - Album - Year - CODEC] Ripped By NAME.ext")
    p()
    p("       Example:")
    p("         ✰ [01 - Nujabes - 羽 Feather - Modal Soul - 2005 - FLAC] Ripped By anon.flac")
    p()
    p("       Full Unicode in filenames — Japanese, Korean, Cyrillic,")
    p("       accented Latin all preserved. Only the 9 Windows-illegal")
    p("       characters (< > : \" / \\ | ? * and null) get replaced with")
    p("       underscore. Soulseek itself is fully UTF-8 capable.")
    p()
    p("       SCOPES (which files):")
    p("         1. Broken only      — status='broken' or in Broken/")
    p("         2. ALL files        — every DB row")
    p("         3. Mismatched only  — current basename != target. Skips")
    p("                                no-op renames.")
    p()
    p("       STRATEGIES (where tags come from):")
    p("         A. Refetch from providers first, THEN rename. Slower but")
    p("            recovers broken-tag files.")
    p("         B. DB only. Skips rows missing artist+title.")
    p()
    p("       FREEFORM EXTRACTION:")
    p("         Titles like 'Helicopter Tune (J Majik VIP Remix)' get split")
    p("         into title='Helicopter Tune' + freeform='J Majik VIP Remix'.")
    p("         Keyword-gated — only triggers on parens containing mix/")
    p("         remix/edit/feat/remaster/year/etc. So 'Strawberry Letter 23'")
    p("         stays intact.")
    p()
    p("       DB INTEGRITY:")
    p("         original_path and original_filename columns are populated")
    p("         on first import and never overwritten (set-once via COALESCE).")
    p("         After rename, the row's path updates but the originals stick.")
    p("         A future 'Undo rename' command can use these to restore.")
    p()
    p("       SAFETY:")
    p("         - Dry-run preview of first 8 renames before any disk op")
    p("         - 'No' is the default confirmation answer")
    p("         - Name conflicts get ' (2)', ' (3)' etc up to (99)")
    p("         - DB update failure after disk rename is logged loudly;")
    p("           run option 3 (Rebuild) to recover")
    p()
    p("       SOULSEEK USERNAME:")
    p("         First Fix-filenames run prompts for username (blank = no")
    p("         suffix). Saved to [organise] soulseek_username so subsequent")
    p("         runs use it automatically.")
    p()

    # ----- ENVIRONMENT -------------------------------------------
    p("ENVIRONMENT")
    p()
    p("       KONSOLE_VERSION, TERM_PROGRAM, TERMINAL_EMULATOR")
    p("           Used for Nerd Font auto-detection (nf=on if known NF-")
    p("           capable terminal). When running under sudo, env may be")
    p("           stripped — set [ui] nerd_font='on' to override.")
    p()
    p("       COLUMNS, LINES")
    p("           Terminal dimensions. The UI re-reads these via")
    p("           os.get_terminal_size() every render frame, so dynamic")
    p("           resize works.")
    p()
    p("       PYTHONUNBUFFERED")
    p("           Recommended set to 1 when redirecting output to a file,")
    p("           otherwise the activity log may appear hung.")
    p()

    # ----- BUGS / LIMITATIONS ------------------------------------
    p("BUGS / KNOWN LIMITATIONS")
    p()
    p("       - MusicBrainz misses underground D&B / bassline / DJ-pool")
    p("         material it doesn't have. Discogs is the better DB for that")
    p("         genre but requires a token to use the search endpoint.")
    p()
    p("       - The 'fake hi-res' bottom-bit detector catches naive 16->24")
    p("         bit-shift upsamples but not properly-dithered resamples.")
    p("         True spectral analysis lives in option 5 (Verify rips) via")
    p("         the vamp lossy-encoding-detector plugin.")
    p()
    p("       - Ripper identification relies on FLAC vendor strings and")
    p("         proprietary tag fingerprints. Post-rip tag editing (Picard,")
    p("         foobar) destroys most of these. Treat results as")
    p("         'best-effort guess' not 'verified provenance'.")
    p()
    p("       - Folder triage classifier doesn't peek INSIDE archives. A")
    p("         .zip of audio looks like a single non-audio file.")
    p()

    # ----- SEE ALSO ----------------------------------------------
    p("SEE ALSO")
    p()
    p("       README.md     Full technical manual (modding / tweaking)")
    p("       config.default.toml")
    p("                     Every config key documented with comments")
    p("       MENU_ITEMS    in zzzzScriptstuff/menu_items.py — adding new")
    p("                     menu options requires no organiser.py change")
    p()
    p("       Picard        https://picard.musicbrainz.org/")
    p("       MusicBrainz   https://musicbrainz.org/")
    p("       Discogs API   https://www.discogs.com/developers")
    p("       OneTagger     https://onetagger.github.io/")
    p()

    # ----- ALIASES ------------------------------------------------
    p("ALIASES")
    p()
    p("       Every option accepts: its number/letter, its full name, a")
    p("       unique prefix, or any registered alias. Examples:")
    p("           'imp' / 'import' / '1' / 'add'        -> Import")
    p("           'org' / 'organize' / 'sort' / 'move'  -> Organise")
    p("           'fetch' / 'tags' / 'mb' / 'lookup'    -> Fetch metadata")
    p("           '?' / 'help' / 'h' / 'wat'            -> this guide")
    p()
    p("       Detail screen for one option: type the option's key followed")
    p("       by '?' (e.g. '7?'). Type 'q' or just press Enter to return.")
    p()
    p("AUTHORS")
    p("       Built by anon for a 4.5+ TiB underground D&B / bassline library.")
    p("       Iterated with assistance from Claude (Anthropic).")
    p()
    p(f"version {__version__}                                          MUSIC-ORGANISER(1)")

    # ----- Page the output through less if available ------------
    text = out.getvalue()
    pager = os.environ.get("PAGER")
    if not pager:
        for candidate in ("less", "more"):
            from shutil import which
            if which(candidate):
                pager = candidate
                break
    if pager and sys.stdout.isatty():
        try:
            import subprocess as _sub
            # -R keeps ANSI colour escapes intact; -X stops less from
            # clearing the screen on exit, leaving the page in scrollback.
            proc = _sub.Popen([pager, "-R", "-X"] if pager == "less" else [pager],
                              stdin=_sub.PIPE)
            proc.communicate(text.encode("utf-8", errors="replace"))
        except (OSError, BrokenPipeError):
            print(text)
    else:
        print(text)
    try:
        sel = input("  press enter to return, or type 'KEY?' for detail: ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if sel.endswith("?") and len(sel) > 1:
        # Show detail for one item
        target_key = sel[:-1].strip()
        match = next((it for it in MENU_ITEMS if it.key == target_key
                       or target_key.lower() in (a.lower() for a in it.aliases)
                       or it.label.lower().startswith(target_key.lower())), None)
        if match is None:
            print(f"  no menu item matches {target_key!r}")
            return
        _show_item_detail(match)


def _show_item_detail(item) -> None:
    """Layman + technical view of one menu item."""
    try:
        from icons import icon
    except ImportError:
        icon = lambda name, fallback=None: fallback or "·"
    print()
    print(f"  ┌─ {icon(item.icon)}  {item.key}. {item.label} ──")
    print()
    print(f"  KEYS: type {item.key!r}, {item.label.lower()!r}, or any of:")
    print(f"    {', '.join(sorted(item.aliases))}")
    print()
    print("  LAYMAN — what does this do?")
    print("  " + "─" * 28)
    # Word-wrap the layman text at ~70 cols
    import textwrap
    for line in textwrap.wrap(item.layman, width=70):
        print(f"    {line}")
    print()
    print("  TECHNICAL — under the hood")
    print("  " + "─" * 27)
    for chunk in item.technical.split("\n"):
        for line in textwrap.wrap(chunk, width=70):
            print(f"    {line}")
    print()
    try:
        input("  press enter to return ")
    except (EOFError, KeyboardInterrupt):
        pass


def cmd_browse(cfg, db) -> None:
    """
    Interactive library browser. Multi-field filter (artist/album/label/
    genre/year/codec) with a free-text search across all of them, plus
    detail popup and CSV export.

    Implementation in browser.py — kept separate because it's a chunky
    Rich-Live UI with its own state and keymap.
    """
    _enter_subscreen(cfg)
    n = db.count()
    if n == 0:
        print()
        print("  The library is empty — nothing to browse.")
        print("  Run option 1 (Import) or 3 (Rebuild database) first.")
        return

    try:
        from browser import run_browser
    except ImportError as e:
        print(f"  ✗ Could not load browser module: {e}")
        print("  (this needs `rich` installed.)")
        return

    run_browser(db, cfg)


def cmd_logo(cfg) -> None:
    """
    Logo font picker. Lists curated fonts numerically, lets the user
    type a number to preview, then accept-or-try-another. Type 'full'
    to see all 571 figlet fonts (long list).
    """
    from config import save_config
    from logo import (
        list_fonts, preview_font, font_works, BUNDLED_BANNERS,
        PYFIGLET_AVAILABLE,
    )

    _enter_subscreen(cfg)

    current = cfg.get("ui", {}).get("logo_font", "default")
    print()
    print(f"  Current logo font: {current}")
    print()

    if not PYFIGLET_AVAILABLE:
        print("  pyfiglet isn't installed — only the bundled default banner is available.")
        print("  Install with:  pip install --user pyfiglet")
        print("    or:          sudo pacman -S python-pyfiglet")
        return

    print("  Available fonts (curated):")
    fonts = list_fonts(curated_only=True)
    for i, name in enumerate(fonts, 1):
        marker = "  <-- current" if name == current else ""
        print(f"    {i:>2}. {name}{marker}")
    print()
    print("  Commands:")
    print("    <number>          set this font as current")
    print("    preview <name>    show this font rendered (any of the 571)")
    print("    full              list ALL 571 pyfiglet fonts (long)")
    print("    q                 back to main menu")
    print()

    while True:
        try:
            choice = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not choice or choice.lower() == "q":
            return

        if choice.lower() == "full":
            all_fonts = list_fonts(curated_only=False)
            print()
            print(f"  All {len(all_fonts)} fonts:")
            # Print in 3-column layout
            cols = 3
            col_width = max(len(f) for f in all_fonts) + 2
            for i in range(0, len(all_fonts), cols):
                row = all_fonts[i : i + cols]
                print("    " + "".join(f"{f:<{col_width}}" for f in row))
            print()
            print("  Use 'preview <name>' to see one, then come back here to set it.")
            continue

        if choice.lower().startswith("preview "):
            target = choice.split(maxsplit=1)[1].strip()
            try:
                art = preview_font(
                    cfg.get("ui", {}).get("logo_text", "music-organiser"),
                    target,
                )
                print()
                print(art)
                print()
            except ValueError as e:
                print(f"  {e}")
            continue

        # `set <name>` — explicit set; resolve against ALL fonts (incl. uncurated)
        if choice.lower().startswith("set "):
            target = choice.split(maxsplit=1)[1].strip()
            # try curated first for fast prefix match, then fall through to all
            resolved, ambig = resolve_selection(target, fonts)
            if resolved is None:
                all_fonts = list_fonts(curated_only=False)
                resolved, ambig = resolve_selection(target, all_fonts)
            if resolved is None:
                if ambig:
                    print(f"  '{target}' is ambiguous — matches:")
                    for m in ambig[:10]:
                        print(f"    {m}")
                    if len(ambig) > 10:
                        print(f"    ...and {len(ambig) - 10} more.")
                else:
                    print(f"  no font matches '{target}'")
                continue
            new_font = resolved
        # numeric pick (curated list only — full has 571 entries, indexing
        # into it would be confusing)
        elif choice.isdigit():
            try:
                idx = int(choice)
                if not (1 <= idx <= len(fonts)):
                    print(f"  out of range — pick 1-{len(fonts)} or use 'set <name>'")
                    continue
                new_font = fonts[idx - 1]
            except ValueError:
                print("  invalid number")
                continue
        # Bare token — try as a direct name (curated first, then all)
        else:
            resolved, ambig = resolve_selection(choice, fonts)
            if resolved is None and not ambig:
                # Not in curated; try the full set.
                all_fonts = list_fonts(curated_only=False)
                resolved, ambig = resolve_selection(choice, all_fonts)
            if resolved is None:
                if ambig:
                    print(f"  '{choice}' is ambiguous — matches:")
                    for m in ambig[:10]:
                        print(f"    {m}")
                    if len(ambig) > 10:
                        print(f"    ...and {len(ambig) - 10} more.")
                else:
                    print(f"  no font matches '{choice}' (try 'preview <name>' for any of the 571 figlet fonts)")
                continue
            new_font = resolved

        # Sanity-check
        if not font_works(new_font):
            print(f"  font '{new_font}' doesn't render — picking something else")
            continue

        # Show what it'll look like
        print()
        try:
            print(preview_font(
                cfg.get("ui", {}).get("logo_text", "music-organiser"),
                new_font,
            ))
        except ValueError as e:
            print(f"  preview failed: {e}")
            continue
        print()

        try:
            confirm = input(f"  set '{new_font}' as the menu logo? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return
        if confirm in ("", "y", "yes"):
            cfg.setdefault("ui", {})["logo_font"] = new_font
            save_config(cfg)
            print(f"  saved logo_font = {new_font}")
            return


def main() -> None:
    if sys.version_info < (3, 11):
        print("This script requires Python 3.11+.")
        print(f"You have: {sys.version}")
        sys.exit(1)

    print(f"music-organiser v{__version__}")

    # Set up the debug log early so we capture EVERYTHING after this point.
    _setup_debug_log()
    log_path = get_debug_log_path()
    if log_path is not None:
        print(f"  debug log: {log_path}")
    _log("startup begin: argv=%r cwd=%r tty=%s", sys.argv, os.getcwd(), sys.stdout.isatty())

    # Pre-flight check: make sure the sibling .py modules we depend on
    # are actually next to organiser.py (or in our scriptstuff folder).
    # If files are missing (e.g. the user copied just organiser.py to
    # another folder), we want to warn ONCE at startup with a clear
    # message rather than crash mid-feature.
    _our_dir = Path(__file__).resolve().parent
    # _MODULES_DIR was set at import time at the top of this file. It's
    # either a path to a nested scriptstuff/-style folder, or None if
    # we're in flat layout.
    _check_dir = _MODULES_DIR if _MODULES_DIR is not None else _our_dir
    _layout = "nested" if _MODULES_DIR is not None else "flat"
    _required = ["config.py", "database.py", "ui.py", "metadata.py",
                  "metadata_lookup.py", "metadata_providers.py",
                  "scanner.py", "importer.py", "indexer.py",
                  "organiser_core.py", "checkpoint.py", "speed.py",
                  "audit.py", "detection.py"]
    _optional = ["lockfile.py", "icons.py", "menu_items.py",
                  "platform_detect.py", "browser.py", "tag_writer.py",
                  "nfo_writer.py", "rip_detection.py", "rip_audio.py",
                  "fake_flac.py", "logo.py", "themes.py", "decoration.py"]
    _missing_required = [m for m in _required if not (_check_dir / m).exists()]
    _missing_optional = [m for m in _optional if not (_check_dir / m).exists()]

    # Detect MIXED layout — modules in BOTH the entry dir and a nested
    # folder. That's a left-over from migration; warn so the user knows
    # to delete the stale flat copies.
    _mixed = []
    if _MODULES_DIR is not None:
        for m in _required + _optional:
            if (_our_dir / m).exists() and (_check_dir / m).exists():
                _mixed.append(m)
        # Skip the obvious false positive: organiser.py itself is at the
        # top level by design.
    if _mixed:
        print()
        print(f"  ⓘ MIXED layout detected:")
        print(f"     Modules in BOTH {_our_dir}")
        print(f"     and {_check_dir}")
        print(f"     The nested copies will win, but the duplicates at the")
        print(f"     top level are stale and clutter your library folder.")
        print(f"     Remove them with:")
        print(f"        cd {_our_dir}")
        for m in _mixed[:8]:
            print(f"        rm {m}")
        if len(_mixed) > 8:
            print(f"        rm <... {len(_mixed) - 8} more>")
        print()

    if _missing_required:
        print()
        if _layout == "nested":
            print(f"  ✗ Required modules missing from {_check_dir}:")
        else:
            print(f"  ✗ Required sibling files are missing from {_our_dir}:")
        for m in _missing_required:
            print(f"      {m}")
        if _missing_optional:
            print()
            print(f"  Also missing (optional, would disable features):")
            for m in _missing_optional:
                print(f"      {m}")
        print()
        print(f"  Re-extract the full music_organiser folder.")
        print(f"  organiser.py alone is not enough — all .py files must be")
        if _layout == "nested":
            print(f"  in {_check_dir.name}/. A safe re-extract:")
        else:
            print(f"  in the same directory. A safe re-extract:")
        print(f"      cd {_our_dir}")
        print(f"      rm *.py                       # only .py files, NOT your music")
        print(f"      rm -rf __pycache__")
        print(f"      unzip -o music_organiser.zip")
        print()
        sys.exit(1)
    if _missing_optional:
        print()
        print(f"  ⓘ Optional modules missing — some features disabled:")
        for m in _missing_optional:
            print(f"      {m}")
        print(f"  Re-extract the full music_organiser folder to enable them.")
        print()

    # Running as root? It's usually unnecessary and creates two parallel
    # config / cache / runtime directories (root's vs. the regular user's).
    # We warn but don't block — there are legitimate uses (e.g. the user's
    # library is on a mount that needs root to access).
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        try:
            owner_uid = _our_dir.stat().st_uid
        except OSError:
            owner_uid = 0
        # If the script's directory belongs to a real user (uid >= 1000),
        # running as root is likely unintentional.
        if owner_uid >= 1000:
            try:
                import pwd
                owner = pwd.getpwuid(owner_uid).pw_name
            except Exception:
                owner = f"uid {owner_uid}"
            print()
            print(f"  ⓘ Running as root (effective uid 0).")
            print(f"     The script lives in a folder owned by '{owner}' — you")
            print(f"     probably don't need sudo. As root:")
            print(f"       • config goes to /root/.config/music-organiser/")
            print(f"         (not /home/{owner}/.config/...)")
            print(f"       • themes you dump go to /root/.config/.../themes/")
            print(f"       • lockfiles go to a different runtime dir, so")
            print(f"         multi-instance detection won't see your normal")
            print(f"         user's instances.")
            print(f"     If you have a reason to need root (USB/NTFS mount), fine,")
            print(f"     this is just an FYI. Otherwise: drop the sudo.")
            print()

    # We can't read install_mode from the config until the config is
    # loaded, but loading the config might pull in tomli_w. Chicken/egg.
    # Resolution: tomllib (read-only) is built-in to 3.11+, so we can
    # always READ a config without tomli_w. We only need tomli_w to write.
    # So: try to load config first to read install_mode, then bootstrap.
    from config import load_config, is_first_run, run_first_time_setup, ensure_default_template

    # Always (re)generate the default template so the example file is
    # always in sync with what's in DEFAULT_CONFIG.
    try:
        ensure_default_template()
    except Exception:
        # If tomli_w isn't installed yet we'll regenerate after bootstrap.
        pass

    pre_cfg = load_config()
    install_mode = pre_cfg.get("deps", {}).get("install_mode", "prompt")
    bootstrap_dependencies(install_mode)

    # Re-write the default template now that tomli_w is definitely available.
    try:
        ensure_default_template()
    except Exception as e:
        print(f"warning: couldn't refresh config.default.toml: {e}")

    # First-run wizard?
    if is_first_run():
        cfg = run_first_time_setup()
    else:
        cfg = load_config()
        # Detect a partial / stale config — the wizard may have been
        # skipped or aborted last time.
        ok, missing = _config_is_complete(cfg)
        if not ok:
            print()
            print("Your config file exists but is incomplete.")
            print(f"  Config:  {Path('~/.config/music-organiser/config.toml').expanduser()}")
            print(f"  Missing: {', '.join(missing)}")
            print()
            try:
                ans = input("Run first-time setup wizard now? [Y/n] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = "n"
            if ans in ("", "y", "yes"):
                cfg = run_first_time_setup()

    # Open DB.
    from database import Database

    # Apply performance tier BEFORE opening the DB so the DB picks up
    # the right pragmas (sync, cache size). Default tier is Ludicrous.
    from speed import apply_speed, set_active_level, get_active_sqlite_pragmas
    speed_id = cfg.get("performance", {}).get("speed_level", "ludicrous")
    applied = apply_speed(speed_id)
    set_active_level(applied.level)
    print(f"  speed: {applied.level.display.upper()}  "
          f"nice={applied.actual_nice}  ionice={applied.actual_ionice}")
    if applied.warnings:
        for w in applied.warnings:
            print(f"     ⓘ  {w}")
        # The nice/ionice tweaks at high tiers want CAP_SYS_NICE (root)
        # to push priority below 0. NOT having them means the process
        # runs at default priority — completely fine for an I/O-bound
        # music indexer. Mention sudo as an option but don't oversell it.
        print(f"     (process priority is at default — that's fine for")
        print(f"      most workloads. sudo only matters if you're racing")
        print(f"      this against other CPU-heavy tasks.)")
    _log("startup speed: level=%s actual_nice=%s actual_ionice=%s",
         applied.level.id, applied.actual_nice, applied.actual_ionice)

    # Merge speed-derived pragmas into config['database'] so the DB picks
    # them up. Order matters: we start with the user's config and let the
    # speed tier fill in any pragma the user hasn't explicitly set.
    # Therefore:
    #   - default config has no `synchronous` / `cache_size_kb` -> speed
    #     tier controls them (Ludicrous gets sync=OFF + 128MB cache).
    #   - user who explicitly sets `database.synchronous = "FULL"` in
    #     config.toml wins — useful if you want safety over speed.
    user_db_cfg = cfg.get("database", {})
    db_pragmas = dict(user_db_cfg)
    speed_pragmas = get_active_sqlite_pragmas()
    for k, v in speed_pragmas.items():
        db_pragmas.setdefault(k, v)

    # Friendly warning for existing users whose saved config carries the
    # pre-v0.14 defaults (synchronous=NORMAL, cache_size_kb=65536) — those
    # values now silently override the speed tier. New installs don't
    # have these keys.
    conflicting = []
    for k, speed_v in speed_pragmas.items():
        if k in user_db_cfg and user_db_cfg[k] != speed_v:
            conflicting.append((k, user_db_cfg[k], speed_v))
    if conflicting:
        print()
        print(f"  ⓘ Your config has database pragmas that override the "
              f"{applied.level.display} speed tier:")
        for k, user_v, tier_v in conflicting:
            print(f"     database.{k} = {user_v!r}  "
                  f"(tier would use {tier_v!r})")
        print(f"     Remove these from your config to let the speed tier "
              f"control them.")
        print(f"     Config file: {Path('~/.config/music-organiser/config.toml').expanduser()}")
        print()

    db_path = Path(cfg["paths"]["database"]).expanduser()

    # Pre-flight: make sure we can actually create the DB directory.
    # Common breakage: user copied a config.toml that another user
    # (typically root) wrote. The `database` path in there points at
    # /root/.local/share/... and we can't write there as a normal user.
    if not db_path.parent.exists():
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            # Recover: figure out the right path for the current user,
            # offer to switch the config + migrate the existing DB if found.
            import getpass
            our_default = Path.home() / ".local" / "share" / "music-organiser" / "library.db"
            print()
            print(f"  ✗ Can't create the database directory:")
            print(f"      {db_path.parent}")
            print(f"    Permission denied — it belongs to another user.")
            print()
            print(f"  Your config has:")
            print(f"      database = \"{db_path}\"")
            print(f"  but you're running as {getpass.getuser()!r}.")
            print()
            # Did the old DB exist? Offer to copy it.
            old_db_exists = False
            try:
                old_db_exists = db_path.exists() and os.access(db_path, os.R_OK)
            except OSError:
                pass
            print(f"  Fix: point the config at your own home directory:")
            print(f"      database = \"{our_default}\"")
            print()
            try:
                ans = input(f"  Update config now? [Y/n] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = "n"
            if ans in ("", "y", "yes"):
                # Update in-memory and on disk
                cfg.setdefault("paths", {})["database"] = str(our_default)
                try:
                    from config import save_config
                    save_config(cfg)
                    print(f"  ✓ config updated.")
                except Exception as e:
                    print(f"  warning: couldn't save config: {e}")
                    print(f"  edit {Path('~/.config/music-organiser/config.toml').expanduser()} manually")
                # Try the migration
                if old_db_exists:
                    print()
                    print(f"  Found an existing database at {db_path}")
                    print(f"  ({db_path.stat().st_size / 1024 / 1024:.1f} MB)")
                    try:
                        copy = input(f"  Copy it to your home so you keep your index? [Y/n] ").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        copy = "n"
                    if copy in ("", "y", "yes"):
                        our_default.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            shutil.copy2(db_path, our_default)
                            # Also try to copy the -wal and -shm sidecars if present
                            for suffix in ("-wal", "-shm"):
                                side = Path(str(db_path) + suffix)
                                if side.exists():
                                    try:
                                        shutil.copy2(side, str(our_default) + suffix)
                                    except OSError:
                                        pass
                            print(f"  ✓ database copied. Run the script again to use it.")
                        except OSError as e:
                            print(f"  ✗ copy failed: {e}")
                            print(f"  you can copy it manually:")
                            print(f"      cp {db_path} {our_default}")
                # Fresh start — re-point db_path and continue
                db_path = our_default
            else:
                print()
                print(f"  To fix manually, edit:")
                print(f"      {Path('~/.config/music-organiser/config.toml').expanduser()}")
                print(f"  and change the [paths] database line to:")
                print(f"      database = \"{our_default}\"")
                sys.exit(1)

    db = Database(db_path, pragmas=db_pragmas)

    # Multi-instance protection. If another writer is using this DB,
    # the lock context will prompt the user (proceed / become reader /
    # quit) before letting us continue. Reader mode skips writer-only
    # commands.
    try:
        from lockfile import InstanceLock
        try:
            with InstanceLock(db_path, mode="writer") as lock:
                # Stash read-only flag in cfg so commands can check it
                cfg["_runtime"] = cfg.get("_runtime", {})
                cfg["_runtime"]["read_only"] = lock.is_read_only()
                if lock.is_read_only():
                    print()
                    print("  ── running in READ-ONLY mode ──")
                    print("  Browse + audits available; import/organise/fetch disabled.")
                    print()
                main_menu(cfg, db)
        except SystemExit:
            raise
    except ImportError:
        # lockfile module missing for some reason — degrade gracefully
        # and just run without the protection
        _log("lockfile module unavailable, skipping instance lock")
        main_menu(cfg, db)
    finally:
        db.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # Ctrl-C anywhere we don't otherwise handle it. Exit clean,
        # no stack trace. The various inner loops that NEED to catch
        # interrupts (animate intro, file copy, etc.) still do so.
        print("\n  (interrupted)")
        sys.exit(130)   # 128 + SIGINT(2), the conventional exit code
