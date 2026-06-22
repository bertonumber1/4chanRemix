"""
icons.py
========

Icon constants for the UI. We use Nerd Font glyphs when available, with
ASCII fallbacks for terminals that can't render them.

Nerd Font glyphs live in the Private Use Area (PUA) of Unicode. They
render only if the user's terminal font is patched (Nerd Fonts is the
common patcher). We detect via a heuristic on environment variables —
there's no perfect probe short of actually trying to render a glyph
and measuring the width, which itself is unreliable across terminals.

Heuristic rules (in order):
  1. NERD_FONT=1 in env  → use glyphs
  2. TERM_PROGRAM=ghostty/wezterm/kitty/Alacritty → likely Nerd Font user
  3. TERM contains 'kitty' → likely Nerd Font user
  4. KONSOLE_VERSION present (Konsole) → likely Nerd Font (your case)
  5. Otherwise → ASCII fallbacks

Override at runtime:
  - Set NERD_FONT=1 to force glyphs
  - Set NERD_FONT=0 to force ASCII
  - Or pass --no-nerd-font / --nerd-font on the command line

What this module does NOT do:
  - Auto-install Nerd Fonts. Fonts are user-environment territory:
    they require touching ~/.local/share/fonts, refreshing fontconfig,
    AND configuring the terminal emulator to use them. We'd be poking
    at the user's terminal config — invasive. We print install hints
    instead and link to the project page.

Reference for glyphs:
  https://www.nerdfonts.com/cheat-sheet
"""

from __future__ import annotations

import os
import sys


def detect_nerd_font() -> bool:
    """
    Return True if the terminal likely has Nerd Font glyphs available.

    Detection order (first conclusive result wins):
      1. NERD_FONT env var override (1/0/true/false/...)
      2. ui.nerd_font config setting from config.toml (auto/on/off)
      3. Terminal heuristic on CURRENT env (KONSOLE_VERSION etc.)
      4. PARENT process env, on Linux — same heuristic but reading
         /proc/<ppid>/environ. Catches the sudo-strips-env case.
      5. Default: ASSUME ON. Nerd Fonts is mainstream enough in 2026
         that defaulting glyphs-on is the better bet. If the user is
         on a terminal that genuinely can't render NF, they'll see
         boxes; they set `ui.nerd_font = "off"` or NERD_FONT=0 once.

    Honest tradeoff with default-ON: terminals without Nerd Font will
    show □ tofu boxes instead of icons. The old default (assume OFF
    unless we see Konsole) was wrong for everybody EXCEPT the handful
    of users with one specific terminal we explicitly knew about.
    """
    # 1. Explicit env override
    nf = os.environ.get("NERD_FONT", "").strip().lower()
    if nf in ("1", "true", "yes", "on"):
        return True
    if nf in ("0", "false", "no", "off"):
        return False

    # 2. Config-file override — survives sudo's environment stripping.
    try:
        cfg_value = _read_config_nerd_font_setting()
        if cfg_value == "on":
            return True
        if cfg_value == "off":
            return False
        # 'auto' or unset → fall through to heuristic
    except Exception:
        pass

    # 3. Current-process env heuristic
    if _env_suggests_nerd_font(os.environ):
        return True

    # 4. Parent-process env heuristic (Linux). sudo and similar wrappers
    # strip env vars before calling our script, so the original user's
    # KONSOLE_VERSION never reaches us. The parent's /proc/<pid>/environ
    # still has the original values.
    try:
        ppid = os.getppid()
        proc_env = _read_proc_environ(ppid)
        if proc_env and _env_suggests_nerd_font(proc_env):
            return True
        # Also try the grandparent — sometimes sudo->shell->python and
        # the immediate parent is just a wrapper.
        try:
            with open(f"/proc/{ppid}/status", "r") as f:
                for line in f:
                    if line.startswith("PPid:"):
                        gppid = int(line.split()[1])
                        gp_env = _read_proc_environ(gppid)
                        if gp_env and _env_suggests_nerd_font(gp_env):
                            return True
                        break
        except (OSError, ValueError):
            pass
    except Exception:
        pass

    # 5. Safe-default-on for unknown terminals. Caveat: terminals on
    # genuinely dumb consoles (`TERM=dumb`, no TTY, etc.) get a NO.
    if os.environ.get("TERM", "").lower() in ("dumb", ""):
        return False
    if not sys.stdout.isatty():
        return False
    # Otherwise assume the user has a modern terminal and Nerd Fonts.
    return True


def _env_suggests_nerd_font(env: dict) -> bool:
    """Look at a dict of env vars (current process or parent) and
    return True if it carries strong evidence of a Nerd-Font-using
    terminal. Used for both the current process and /proc/<ppid>/environ."""
    if env.get("KONSOLE_VERSION") or env.get("KONSOLE_DBUS_SESSION"):
        return True
    tp = (env.get("TERM_PROGRAM") or "").lower()
    if any(t in tp for t in ("ghostty", "wezterm", "kitty", "alacritty",
                              "rio", "iterm")):
        return True
    term = (env.get("TERM") or "").lower()
    if "kitty" in term:
        return True
    # WezTerm and Alacritty also set their own markers
    if env.get("WEZTERM_PANE") or env.get("ALACRITTY_LOG"):
        return True
    return False


def _read_proc_environ(pid: int) -> dict | None:
    """Read /proc/<pid>/environ on Linux and return a dict of env vars.
    Returns None on non-Linux, permission denied, or any error.

    /proc/<pid>/environ contains the original env vars passed to the
    process at exec time, NULL-separated. Reading another user's process
    typically requires being that user or root — but since we're often
    INVOKED via sudo (so we're root), we can read the original user's
    shell environment that sudo stripped from our own."""
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            raw = f.read()
    except (OSError, FileNotFoundError):
        return None
    if not raw:
        return None
    out: dict[str, str] = {}
    for entry in raw.split(b"\x00"):
        if not entry or b"=" not in entry:
            continue
        try:
            key, _, val = entry.partition(b"=")
            out[key.decode("utf-8", "replace")] = val.decode("utf-8", "replace")
        except Exception:
            continue
    return out


def _read_config_nerd_font_setting() -> str | None:
    """
    Cheap read of just the ui.nerd_font field from config.toml.
    Returns 'on' / 'off' / 'auto' / None. Does NOT import config.py
    because this function gets called early.

    When running as root via sudo, we still want to find the original
    user's config — check $SUDO_USER's home first, then root's own
    XDG_CONFIG_HOME, then fall back to root's $HOME/.config.
    """
    from pathlib import Path
    candidates: list[Path] = []
    # Real user's config (when running via sudo)
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        try:
            import pwd
            user_home = Path(pwd.getpwnam(sudo_user).pw_dir)
            candidates.append(user_home / ".config" / "music-organiser" / "config.toml")
        except Exception:
            pass
    # Current effective user's config
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        candidates.append(Path(xdg) / "music-organiser" / "config.toml")
    candidates.append(Path.home() / ".config" / "music-organiser" / "config.toml")

    for path in candidates:
        if not path.exists():
            continue
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib   # type: ignore
            except ImportError:
                return None
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
            val = data.get("ui", {}).get("nerd_font")
            if val is None:
                continue
            val = str(val).lower().strip()
            if val in ("on", "true", "yes", "1"):
                return "on"
            if val in ("off", "false", "no", "0"):
                return "off"
            return "auto"
        except Exception:
            continue
    return None


# Each constant has a Nerd Font glyph and an ASCII fallback. Resolve via
# the `icon(...)` helper which picks one based on detection.

# Music + library
_GLYPHS = {
    # Files & content
    "music":        ("\ue0a3", "♪"),   # nf-pl-line_number
    "album":        ("\uf501", "[A]"),
    "artist":       ("\uf007", "[U]"),  # user
    "playlist":     ("\uf0cb", "[P]"),
    "folder":       ("\uf07b", "[+]"),
    "folder_open":  ("\uf07c", "[+]"),
    "file":         ("\uf15b", "[f]"),
    "tag":          ("\uf02b", "[t]"),
    "search":       ("\uf002", "/"),
    "filter":       ("\uf0b0", "*"),

    # Status
    "check":        ("\uf058", "[x]"),
    "cross":        ("\uf057", "[X]"),
    "warning":      ("\uf071", "[!]"),
    "info":         ("\uf05a", "[i]"),
    "spinner":      ("\uf110", "*"),

    # Audio formats
    "flac":         ("\uf001", "FLAC"),
    "mp3":          ("\uf001", "MP3"),
    "lossless":     ("\uf09c", "<*>"),  # unlock
    "lossy":        ("\uf023", "[L]"),  # lock

    # Database / data
    "database":     ("\uf1c0", "[DB]"),
    "table":        ("\uf0ce", "[#]"),
    "hard_drive":   ("\uf0a0", "[HD]"),     # nf-fa-hdd
    "disk":         ("\uf0a0", "[HD]"),     # alias
    "tag_count":    ("\uf02c", "[#t]"),     # nf-fa-tags (multiple)

    # Speed tiers — semantic; default snail/horse/rocket progression
    "snail":        ("\uf6c0", "[slow]"),     # nf-mdi-snail
    "turtle":       ("\uf726", "[ok]"),
    "rabbit":       ("\uf708", "[fast]"),
    "horse":        ("\uf6f0", "[fast]"),
    "rocket":       ("\uf135", "[zoom]"),
    "lightning":    ("\uf0e7", "[plaid]"),    # nf-fa-bolt

    # Time / progress
    "clock":        ("\uf017", "@"),
    "hourglass_full":  ("\uf251", "(O)"),
    "hourglass_empty": ("\uf254", "( )"),
    "stopwatch":    ("\uf2f2", "(S)"),
    "arrow_right":  ("\uf061", "->"),
    "arrow_left":   ("\uf060", "<-"),
    "dot":          ("\uf444", "."),

    # Network / providers
    "world":        ("\uf0ac", "@"),
    "download":     ("\uf019", "v"),
    "upload":       ("\uf093", "^"),
    "musicbrainz":  ("\uf001", "MB"),

    # Action
    "play":         ("\uf04b", ">"),
    "pause":        ("\uf04c", "||"),
    "stop":         ("\uf04d", "[]"),
    "rec":          ("\uf111", "(o)"),

    # Logos
    "linux":        ("\uf17c", "[lin]"),
    "apple":        ("\uf179", "[mac]"),
    "windows":      ("\uf17a", "[win]"),
    "github":       ("\uf09b", "[gh]"),
    "config":       ("\uf013", "(c)"),
    "wrench":       ("\uf0ad", "{w}"),
    "trash":        ("\uf014", "[del]"),
    "package":      ("\uf187", "[pkg]"),
}


# Clock spinner sequence — Nerd Fonts have hour-by-hour clock glyphs that
# we cycle through for an animated clock effect.
CLOCK_FRAMES = [
    "\uf143", "\uf144", "\uf145", "\uf146", "\uf147", "\uf148",
    "\uf149", "\uf14a", "\uf14b", "\uf14c", "\uf14d", "\uf14e",
]
CLOCK_FRAMES_ASCII = ["|", "/", "-", "\\"]

# Generic spinner
SPINNER_FRAMES_NF = ["\uf251", "\uf252", "\uf253", "\uf254"]   # hourglass cycle
SPINNER_FRAMES_ASCII = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


_USE_NF: bool | None = None


def use_nerd_font() -> bool:
    """Cached detection result. Call this once at startup if you want
    to lock it in."""
    global _USE_NF
    if _USE_NF is None:
        _USE_NF = detect_nerd_font()
    return _USE_NF


def force_nerd_font(value: bool | None) -> None:
    """Override detection at runtime — used by --nerd-font / --no-nerd-font
    CLI flags or test fixtures."""
    global _USE_NF
    _USE_NF = value


def icon(name: str, fallback: str | None = None) -> str:
    """Return the icon glyph for the given name. Uses Nerd Font if
    available, else the ASCII fallback (overridable via the `fallback`
    parameter)."""
    entry = _GLYPHS.get(name)
    if entry is None:
        return fallback or "?"
    glyph, ascii_fb = entry
    if use_nerd_font():
        return glyph
    return fallback if fallback is not None else ascii_fb


def clock_frame(tick: int) -> str:
    """Return one frame of an animated clock for the given tick. Cycles
    automatically through all available frames."""
    frames = CLOCK_FRAMES if use_nerd_font() else CLOCK_FRAMES_ASCII
    return frames[tick % len(frames)]


def spinner_frame(tick: int) -> str:
    frames = SPINNER_FRAMES_NF if use_nerd_font() else SPINNER_FRAMES_ASCII
    return frames[tick % len(frames)]


def codec_icon(codec: str) -> str:
    """Pick an icon based on file format. Lossless gets unlock, lossy
    gets lock — matches the music-fan vocabulary."""
    if not codec:
        return icon("file")
    cl = codec.lower()
    lossless = {"flac", "wav", "aiff", "aif", "ape", "wv", "alac",
                "dsf", "dff"}
    if cl in lossless:
        return icon("lossless")
    return icon("lossy")


def os_icon(os_id: str) -> str:
    """Linux/Mac/Windows logo per detect_os() result."""
    if os_id in ("darwin", "macos"):
        return icon("apple")
    if os_id == "windows":
        return icon("windows")
    return icon("linux")


def speed_icon(tier: str) -> str:
    """
    Animal/object metaphor for the speed tier. Slowest = snail, fastest
    = lightning. Used in menu hints to give an at-a-glance sense of
    where you are on the tradeoff.
    """
    t = (tier or "").lower()
    if t in ("light", "light_speed", "lightspeed", "safe"):
        return icon("snail")
    if t in ("ridiculous", "ridiculous_speed"):
        return icon("rabbit")
    if t in ("ludicrous", "ludicrous_speed"):
        return icon("rocket")
    if t in ("plaid", "plaid_speed"):
        return icon("lightning")
    return icon("turtle")  # 'default' / unknown


def install_hint() -> str:
    """Multi-line message for users without Nerd Fonts."""
    return (
        "  music-organiser uses icons from Nerd Fonts when available.\n"
        "  You're seeing ASCII fallbacks because:\n"
        "    - your terminal isn't recognised as a Nerd-Font emulator, or\n"
        "    - your monospace font isn't a Nerd-Font-patched one.\n"
        "\n"
        "  To get icons:\n"
        "    1. Install Nerd Fonts: https://www.nerdfonts.com/font-downloads\n"
        "       (e.g. JetBrainsMono Nerd Font, FiraCode Nerd Font)\n"
        "    2. Set your terminal to use that font\n"
        "    3. Restart music-organiser, or set NERD_FONT=1 to force on\n"
    )
