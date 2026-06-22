"""
config.py
=========

Handles persistent configuration for the music organiser.

- Single source of truth for defaults: DEFAULT_CONFIG below.
- Loads from $XDG_CONFIG_HOME/music-organiser/config.toml (or ~/.config/...).
- Falls back to ./config.toml in the project dir if that exists.
- On first run (no config file anywhere), launches an interactive wizard
  that asks for source paths, the destination root, and DB location, then
  writes a fresh config.toml.
- Also writes a `config.default.toml` next to this module on import if it
  doesn't exist, so you always have a clean template to crib from.

Why TOML instead of JSON: comments. You're going to want to hand-edit this.
Python 3.11+ has tomllib built in. We use `tomli_w` to write — falls back
to a tiny hand-rolled writer if tomli_w isn't installed (it's a soft dep).
"""

from __future__ import annotations

import os
import sys
import shutil
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

# Writing TOML — tomllib only reads. tomli_w is the canonical writer.
try:
    import tomli_w  # type: ignore
    _HAS_TOMLI_W = True
except ImportError:
    _HAS_TOMLI_W = False


# =============================================================================
# DEFAULT CONFIG — SINGLE SOURCE OF TRUTH
# =============================================================================
#
# Every key the program reads must have a default here. If you add a new
# config option, add it here AND regenerate config.default.toml (delete it
# and run the program once).
#
# Paths in `sources` and `destination_root` are intentionally LEFT EMPTY
# in defaults — the first-run wizard fills them in. The default.toml file
# shipped to users will have example paths commented in.

DEFAULT_CONFIG: dict[str, Any] = {
    "paths": {
        # Where to import FROM. List of directories that will be walked
        # recursively for audio files. The first-run wizard populates this
        # with the four paths you mentioned; you can edit any time.
        "sources": [],
        # Where the organised tree lives.
        # Layout: <destination_root>/<quality_tier>/<label>/<artist - album - year>/track.flac
        "destination_root": "",
        # Where the SQLite database file lives. The directory will be created.
        "database": "~/.local/share/music-organiser/library.db",
        # Where to dump files that are unreadable / corrupted.
        # Relative paths are resolved against destination_root.
        "broken_folder": "Broken",
        # Subdirectory under destination_root for the main organised content.
        # Set to "" if you don't want a quality-tier subdir.
        "high_quality_folder": "High Quality",
        "low_quality_folder": "Shit Quality",
    },
    "import": {
        # "copy" leaves originals in place. "move" deletes from source after
        # a successful copy + DB write. Start with "copy" until you trust it.
        "mode": "copy",
        # File extensions to consider audio. Lowercase, with leading dot.
        "audio_extensions": [
            ".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav",
            ".aiff", ".aif", ".ape", ".wv", ".alac", ".dsf", ".dff",
        ],
        # Files that get copied/moved alongside the audio of each album.
        # The "everything in the folder that isn't audio and isn't junk"
        # rule is applied in addition to this list when
        # `take_all_non_audio` is true (default).
        #
        # Why an explicit list AT ALL given take_all_non_audio? Two
        # reasons: (a) if someone disables take_all_non_audio for a strict
        # workflow, this gives a sane allowlist; (b) the list documents
        # the kinds of things we expect to see (art, logs, cues, videos,
        # signatures, archives, playlists, PDFs, notes).
        "extras_extensions": [
            # cover / liner art
            ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".svg",
            # rip metadata
            ".log", ".cue", ".nfo", ".md5", ".sfv",
            # text / docs
            ".txt", ".md", ".pdf", ".rtf",
            # playlists
            ".m3u", ".m3u8", ".pls",
            # videos (e.g. bonus content)
            ".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".wmv", ".flv",
            # signatures / hashes
            ".sig", ".asc", ".sha256", ".sha1",
            # archives bundled with the album
            ".zip", ".rar", ".7z",
            # subtitles / lyrics
            ".lrc", ".srt", ".vtt",
        ],
        # When true, move/copy EVERY non-audio file in the album folder
        # to the destination, not just files matching extras_extensions.
        # OS junk (Thumbs.db, .DS_Store, etc — see junk_filenames below)
        # is always skipped.
        "take_all_non_audio": True,
        # Filename patterns that NEVER get moved, regardless of extension.
        # Case-insensitive match. Useful for OS-generated cruft.
        "junk_filenames": [
            ".ds_store", "thumbs.db", "desktop.ini", "icon\r",
            ".directory", ".localized",
        ],
        # Skip hidden directories (starting with '.') when scanning.
        "skip_hidden": True,
        # Follow symlinks when walking. Usually NO — loops are bad.
        "follow_symlinks": False,
        # If true, files smaller than this many bytes are ignored as junk.
        "min_size_bytes": 4096,
    },
    "organise": {
        # Heuristics for deciding "this album is a mix/compilation, not a
        # solo album by one artist". When this fires, the layout becomes:
        #   <label>/<album_name>/<track> - <artist> - <title>.ext
        # instead of:
        #   <label>/<artist> - <album> - <year>/<track> - <title>.ext

        # If the percentage of distinct artists across an album's tracks
        # exceeds this, treat as mix. 0.5 = >50% of tracks have a different
        # artist than the album's most-common artist.
        "mix_artist_diversity_threshold": 0.5,
        # Album-artist tag values that always indicate a compilation.
        "various_artists_tags": [
            "various artists", "various", "va", "v.a.",
            "verschiedene", "compilation",
        ],
        # Keywords in the album name that suggest a mix even if tags look solo.
        "mix_keywords": [
            "mix", "compilation", "presents", "selected by",
            " dj ", "mixed by", "essential mix", "podcast",
        ],
        # If label is missing, use this string. Bare "Unknown" gets tedious
        # to navigate — change to e.g. "_Unknown Label" to sort to top/bottom.
        "unknown_label": "Unknown Label",
        "unknown_artist": "Unknown Artist",
        "unknown_album": "Unknown Album",
        # Sanitise filenames: replace these chars with underscore.
        # Linux only really hates '/' and NUL, but we strip Windows-illegal
        # too so the tree is portable to a backup drive on any OS.
        "illegal_path_chars": "<>:\"/\\|?*\x00",
        # Max length for a single path component (folder or filename without
        # extension). Most Linux filesystems allow 255 bytes per component.
        "max_component_length": 200,
    },
    "database": {
        # SQLite pragmas applied on connect. journal_mode (WAL) is the
        # one structural choice and needs to be consistent across runs;
        # the others (synchronous, cache_size_kb) are CONTROLLED BY the
        # performance.speed_level tier (see speed.py). To force a
        # specific value, add it here — explicit config wins over the
        # speed-tier defaults.
        "journal_mode": "WAL",
        "temp_store": "MEMORY",
        # If true, runs ANALYZE periodically to keep query planner happy.
        "auto_analyse": True,
    },
    "deps": {
        # Behaviour when a required Python package is missing.
        # "prompt" = ask before installing.
        # "auto"   = install silently (uses pacman first, then pip --user).
        # "never"  = error out and exit.
        "install_mode": "prompt",
        # Required packages — friendly name -> (import name, pacman pkg, pip pkg)
        # This is read by organiser.py at startup.
    },
    "performance": {
        # Performance tier applied at startup. Controls CPU nice level,
        # I/O scheduling priority, SQLite durability sync, GC behaviour
        # in hot loops, and DB transaction batch size.
        #
        # Available tiers (Spaceballs canon, in order):
        #   sub-light    — IDLE I/O priority, polite to other processes
        #   light-speed  — stock priorities, NORMAL sync
        #   ridiculous   — elevated I/O, GC off in hot loops
        #   ludicrous    — DEFAULT. I/O priority 0, sync=OFF, big batches
        #   plaid        — realtime I/O scheduling, REQUIRES sudo
        #
        # When this tier asks for things you don't have permission for
        # (negative nice without root, RT I/O class without root), the
        # process gracefully falls back to the next tier and prints a
        # warning. The startup line in the terminal shows what actually
        # took effect.
        #
        # Change via menu option 'p' (performance) or by editing this
        # value directly. Effect applies on next launch.
        "speed_level": "ludicrous",
    },
    "ui": {
        # Use Rich progress bars + multi-panel live UI if Rich is installed.
        # Falls back to plain text if it isn't. The auto-installer will
        # offer to install Rich.
        "use_rich": True,
        # ASCII-art banner shown above the menu. 'graffiti' is a 6-line
        # tag-style font that fits at narrower terminal widths (~100 cols),
        # which is why it's the default. 'bigmono12' is wider/blockier but
        # wraps below ~150 cols. 'default' uses the bundled custom banner
        # that ships with the project. Any pyfiglet font name regenerates
        # the title. Run menu option 'l' (Logo) to preview every font.
        "logo_font": "graffiti",
        # Title text fed to pyfiglet when logo_font is not 'default'.
        # Ignored for the bundled banner since that's pre-rendered.
        "logo_text": "music-organiser",
        # Footer line below the banner. Set to "" to hide. Pre-set to the
        # /mu/ board URL because that's where you are.
        "logo_footer": "https://boards.4chan.org/mu/",
        # Animate the main menu briefly on entry — gradient rule lines
        # march sideways for ~1 second when you return to the menu. Pure
        # eye candy; set to false if you find it slows you down.
        "animate_menu": True,
        # How long the menu intro animates, in seconds.
        "animate_menu_duration": 1.2,
        # Continuous animation while waiting for input. Uses cursor save/
        # restore tricks to redraw just the header bands while input()
        # blocks on the bottom row. May flicker on some terminals; set to
        # false if you see artefacts.
        "animate_while_input": True,
        # Paint the theme background across the full terminal width (rather
        # than capping at 70 cols). Has no effect on themes without a
        # background colour. Looks dramatic with 4chan/trollface.
        "fullwidth_background": True,
        # Theme name for the live UI. See ui.THEMES for the full list.
        # 41 built-in across several flavour groups:
        #   originals:   cyber, matrix, synthwave, mono, fire
        #   nature:      ocean, forest, sunset, autumn, winter, spring
        #   cyberpunk:   vaporwave, tron, neon_pink, miami, hacker
        #   monochrome:  amber, phosphor, newsprint, blueprint
        #   dark/gothic: dracula, nord, solarized, gruvbox
        #   bold:        crimson  (red/white/black, very high contrast)
        #   imageboard:  4chan    (cream background + maroon text)
        #   memes:       pepe, doge, gigachad, trollface, nyancat,
        #                stonks, catjam, bonk
        #   MLP:         rainbowdash, twilightsparkle, applejack,
        #                fluttershy, pinkiepie, rarity, spike
        "theme": "rainbowdash",
        # Animation framerate. 20 is buttery; 10 is fine; <5 looks janky.
        # Higher = more CPU.
        "refresh_per_second": 20,
        # Force Rich to emit ANSI colour codes even if it thinks the
        # terminal can't handle them. Set this to true if your menu/import
        # looks plain white despite a configured theme — Rich's auto-detect
        # is too conservative on some terminals (e.g. when stdout looks
        # redirected even though it isn't).
        "force_terminal": False,
        # Colour depth: "auto" (let Rich decide), "standard" (8 colours),
        # "256" (256 colours), "truecolor" (24-bit). If force_terminal is
        # true, set this to "truecolor" for the best look.
        "color_system": "auto",
        # How often to update the plain-text fallback, in files processed.
        "progress_update_interval": 5,
    },
}


# =============================================================================
# PATHS
# =============================================================================

def _config_dir() -> Path:
    """XDG-compliant config directory."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "music-organiser"
    return Path.home() / ".config" / "music-organiser"


def _data_dir() -> Path:
    """XDG-compliant data directory (for the DB by default)."""
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "music-organiser"
    return Path.home() / ".local" / "share" / "music-organiser"


def config_path() -> Path:
    """
    Where to read/write the user's config.

    Priority:
      1. ./config.toml in the project dir (if it exists) — handy for dev.
      2. $XDG_CONFIG_HOME/music-organiser/config.toml — production location.
    """
    project_local = Path(__file__).parent / "config.toml"
    if project_local.exists():
        return project_local
    return _config_dir() / "config.toml"


def default_template_path() -> Path:
    """Path to config.default.toml — always next to this module."""
    return Path(__file__).parent / "config.default.toml"


# =============================================================================
# DEEP MERGE — so adding a new key in DEFAULT_CONFIG doesn't break old configs
# =============================================================================

def _deep_merge(defaults: dict, overrides: dict) -> dict:
    """
    Merge `overrides` on top of `defaults`. Nested dicts are merged
    recursively; lists and scalars from overrides win outright.

    This means a user can edit only the keys they care about and we
    fill in the rest from defaults.
    """
    out = dict(defaults)
    for k, v in overrides.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# =============================================================================
# TOML WRITING — tomli_w if available, else a small hand-rolled writer
# =============================================================================

def _toml_quote(s: str) -> str:
    """Quote a string for TOML — handle backslashes and double quotes."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _write_toml_basic(data: dict, fp) -> None:
    """
    Minimal TOML writer for nested dicts of scalars, lists, and sub-dicts.
    Used only if tomli_w isn't installed. Sufficient for our config shape.
    """
    def emit_value(v: Any) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, str):
            return _toml_quote(v)
        if isinstance(v, list):
            return "[" + ", ".join(emit_value(x) for x in v) + "]"
        raise TypeError(f"can't TOML-encode {type(v).__name__}: {v!r}")

    def emit_section(d: dict, prefix: str = "") -> None:
        # First: scalars and lists at this level.
        scalars = {k: v for k, v in d.items() if not isinstance(v, dict)}
        sub_dicts = {k: v for k, v in d.items() if isinstance(v, dict)}

        if prefix:
            fp.write(f"\n[{prefix}]\n")
        for k, v in scalars.items():
            fp.write(f"{k} = {emit_value(v)}\n")
        for k, v in sub_dicts.items():
            new_prefix = f"{prefix}.{k}" if prefix else k
            emit_section(v, new_prefix)

    emit_section(data)


def write_toml(data: dict, path: Path) -> None:
    """Write a config dict to a TOML file, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if _HAS_TOMLI_W:
        with open(path, "wb") as fp:
            tomli_w.dump(data, fp)
    else:
        with open(path, "w", encoding="utf-8") as fp:
            fp.write("# Music Organiser config — written without tomli_w.\n")
            fp.write("# Install python-tomli-w for nicer formatting.\n")
            _write_toml_basic(data, fp)


# =============================================================================
# LOAD / SAVE
# =============================================================================

def load_config() -> dict:
    """
    Load config from disk, merged on top of DEFAULT_CONFIG.

    If no config file exists, returns DEFAULT_CONFIG unchanged. The caller
    is responsible for detecting "first run" via `is_first_run()` and
    invoking the wizard.
    """
    cfg_path = config_path()
    if not cfg_path.exists():
        return _deep_merge(DEFAULT_CONFIG, {})

    try:
        with open(cfg_path, "rb") as fp:
            user_cfg = tomllib.load(fp)
    except Exception as e:
        print(f"WARNING: could not parse {cfg_path}: {e}", file=sys.stderr)
        print("Falling back to defaults. Fix the file and restart.", file=sys.stderr)
        return _deep_merge(DEFAULT_CONFIG, {})

    return _deep_merge(DEFAULT_CONFIG, user_cfg)


def _strip_private(d: dict) -> dict:
    """
    Recursively drop keys that start with '_' from a nested dict.
    Used before TOML serialisation so transient runtime state (cached
    Console objects, etc) doesn't leak into the saved config.
    """
    out: dict = {}
    for k, v in d.items():
        if isinstance(k, str) and k.startswith("_"):
            continue
        if isinstance(v, dict):
            out[k] = _strip_private(v)
        else:
            out[k] = v
    return out


def save_config(cfg: dict) -> Path:
    """Persist a config dict to the user's config path. Returns the path."""
    cfg_path = config_path()
    write_toml(_strip_private(cfg), cfg_path)
    return cfg_path


def is_first_run() -> bool:
    """True if no config file exists in either the project dir or XDG_CONFIG_HOME."""
    project_local = Path(__file__).parent / "config.toml"
    xdg_local = _config_dir() / "config.toml"
    return not (project_local.exists() or xdg_local.exists())


def ensure_default_template() -> Path:
    """
    Make sure config.default.toml exists next to this module. Always
    overwritten on every run so it stays in sync with DEFAULT_CONFIG —
    that's the contract: the default template == the in-code defaults.
    """
    path = default_template_path()
    # Build a "fully populated" example with the suggested source paths
    # filled in so the user has a complete template, even though the
    # actual DEFAULT_CONFIG leaves them empty.
    example = _deep_merge(DEFAULT_CONFIG, {
        "paths": {
            "sources": [
                "/mnt/Expansion/rips/",
                "/mnt/Expansion/flaccers/",
                "/mnt/Expansion/downloads/",
            ],
            "destination_root": "/mnt/Expansion/Organised/",
            "orphan_folder": "orphaned bonus files",
            "out_of_library_dest": "/mnt/Expansion/downloads/OrganiserStuff/",
        },
    })
    write_toml(example, path)
    return path


# =============================================================================
# FIRST-RUN WIZARD
# =============================================================================

def _ask(prompt: str, default: str = "") -> str:
    """Prompt with optional default, return stripped answer (or default)."""
    if default:
        full = f"{prompt} [{default}]: "
    else:
        full = f"{prompt}: "
    try:
        ans = input(full).strip()
    except EOFError:
        return default
    return ans or default


def _ask_yes_no(prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    while True:
        ans = _ask(f"{prompt} ({d})").lower()
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("  please answer y or n")


def _ask_path_list(prompt: str, suggestions: list[str]) -> list[str]:
    """Ask the user to confirm/edit a list of paths."""
    print(f"\n{prompt}")
    print("  Suggested paths (from your message):")
    for i, s in enumerate(suggestions, 1):
        exists = "✓" if Path(s).expanduser().exists() else "✗ (not currently mounted?)"
        print(f"    {i}. {s}  {exists}")
    print("  Options:")
    print("    - Press Enter to accept all suggested paths.")
    print("    - Type numbers to keep, comma-separated (e.g. '1,3').")
    print("    - Type 'edit' to enter paths manually one-per-line.")
    ans = _ask("  Choice").lower()

    if not ans:
        return list(suggestions)

    if ans == "edit":
        print("  Enter source paths, one per line. Empty line to finish.")
        result: list[str] = []
        while True:
            line = _ask(f"  path #{len(result) + 1}")
            if not line:
                break
            result.append(line)
        return result

    # numeric selection
    try:
        idxs = [int(x.strip()) for x in ans.split(",") if x.strip()]
        return [suggestions[i - 1] for i in idxs if 1 <= i <= len(suggestions)]
    except ValueError:
        print("  didn't understand, using all suggestions")
        return list(suggestions)


def run_first_time_setup() -> dict:
    """
    Interactive setup wizard. Returns a config dict and saves it to disk.
    Called automatically by organiser.py if is_first_run() returns True.
    """
    print()
    print("=" * 70)
    print("  music-organiser — first-run setup")
    print("=" * 70)
    print()

    # ----- OS detection -------------------------------------------------------
    # Identify the host OS so dep install commands and a few default paths
    # adapt automatically. Fails-soft: if detection is uncertain, we ask.
    try:
        from platform_detect import detect_os, manual_os_prompt
        os_info = detect_os()
        if os_info.id == "unknown":
            print("  Could not auto-detect your OS.")
            os_info = manual_os_prompt()
        else:
            print(f"  Detected: {os_info.pretty_name}  "
                  f"(pkg manager: {os_info.family})")
            try:
                ok = input("  Is this correct? [Y/n] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ok = ""
            if ok in ("n", "no"):
                os_info = manual_os_prompt()
        # Stash it into config so other commands can reuse without re-detecting
        # (and so the user can override it manually by editing config.toml).
        # Lives under [host] section to keep [paths]/[import] clean.
    except Exception:
        os_info = None
    print()

    print("This will create a config file. You can edit it any time at:")
    print(f"  {config_path()}")
    print()
    print("A clean template is also at:")
    print(f"  {default_template_path()}")
    print()

    cfg = _deep_merge(DEFAULT_CONFIG, {})

    # Stash OS info in the cfg under a [host] section
    if os_info is not None:
        cfg["host"] = {
            "os_id": os_info.id,
            "pretty_name": os_info.pretty_name,
            "pkg_manager": os_info.family,
        }

    # --- SOURCES -----------------------------------------------------------
    # Use platform-appropriate suggested paths so a Mac/Windows user
    # doesn't see Linux-only paths in the suggestion.
    if os_info is not None and os_info.is_macos:
        suggested_sources = [str(Path.home() / "Music"),
                             "/Volumes/Music",
                             str(Path.home() / "Downloads")]
    elif os_info is not None and os_info.is_windows:
        # Windows users won't see this in a UI, but in case they run from cmd
        home = Path.home()
        suggested_sources = [str(home / "Music"), str(home / "Downloads")]
    elif os_info is not None and os_info.is_linux:
        suggested_sources = [
            "/mnt/Expansion/rips/",
            "/mnt/Expansion/flaccers/",
            "/mnt/Expansion/downloads/",
        ]
    else:
        suggested_sources = [str(Path.home() / "Music")]
    cfg["paths"]["sources"] = _ask_path_list(
        "Where should we IMPORT FROM?",
        suggested_sources,
    )

    # --- DESTINATION -------------------------------------------------------
    print()
    if os_info is not None and os_info.is_macos:
        default_dest = str(Path.home() / "Music" / "Organised")
    elif os_info is not None and os_info.is_windows:
        default_dest = str(Path.home() / "Music" / "Organised")
    elif os_info is not None and os_info.is_linux:
        default_dest = "/mnt/Expansion/Organised/"
    else:
        default_dest = str(Path.home() / "Music" / "Organised")
    cfg["paths"]["destination_root"] = _ask(
        "Where should the organised tree LIVE?",
        default_dest,
    )

    # --- DATABASE ----------------------------------------------------------
    print()
    default_db = str(_data_dir() / "library.db")
    cfg["paths"]["database"] = _ask(
        "Where should the SQLite database file go?",
        default_db,
    )

    # --- IMPORT MODE -------------------------------------------------------
    print()
    print("Import mode:")
    print("  'copy' — leave originals in place. Safer for first runs.")
    print("  'move' — delete from source after successful copy.")
    cfg["import"]["mode"] = _ask("Mode (copy/move)", "copy").lower()
    if cfg["import"]["mode"] not in ("copy", "move"):
        print(f"  unrecognised, defaulting to 'copy'")
        cfg["import"]["mode"] = "copy"

    # --- SAVE --------------------------------------------------------------
    saved_to = save_config(cfg)
    print()
    print(f"✓ Config written to {saved_to}")
    print()
    return cfg
