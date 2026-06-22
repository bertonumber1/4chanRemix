"""
ui.py
=====

Live multi-panel TUI for long-running imports / index runs.

Layout (Radare2-inspired):

  ┌── music-organiser ── theme: cyber ─── 14:32:07 ──────────────────────────┐
  ├──────── Folder ──────────────┬──────────── File ─────────────────────────┤
  │ ⠧ Aphex Twin/SAW 85-92       │ ⠧ 03 - Xtal.flac           (3 of 12)      │
  │ /run/media/anon/.../rips/... │ 4.2 MiB · flac · 16/44.1                  │
  ├──────── Grabbing ────────────┼──────── Organising to ────────────────────┤
  │ artist:    Aphex Twin        │ /run/media/anon/Expansion/Organised/      │
  │ album:     Selected Ambient… │   High Quality/R&S Records/               │
  │ label:     R&S Records       │     Aphex Twin - Selected Ambient W… /    │
  │ year:      1992              │       03 - Xtal.flac                      │
  │ track:     03                │                                           │
  ├──────── Progress ────────────┴───────────────────────────────────────────┤
  │ ████████████████████░░░░░░░░░░░░  62%   1,247 / 2,003   ETA  04:12       │
  ├──────── Activity ────────────────────────────────────────────────────────┤
  │ 14:32:01  imported  Boards of Canada - Music Has the Right to Children    │
  │ 14:32:03  imported  Burial - Untrue                                       │
  │ 14:32:05  duplicate Squarepusher - Hard Normal Daddy (skipped)            │
  │ 14:32:06  imported  Autechre - Tri Repetae                                │
  └──────────────────────────────────────────────────────────────────────────┘

Design notes:
- All state lives in `ImportState`. The importer pushes data via
  `LiveImportUI.update(...)`; the render loop reads from it on each tick.
  This decouples animation rate from data rate — the importer can be
  slow or bursty, the UI stays smooth at 20 fps.
- Themes are just colour palettes (Theme dataclass). Adding a new theme
  is one block of `Theme(...)` and one entry in THEMES.
- Long paths *scroll horizontally* rather than truncating, so you can
  read them even when they're wider than the panel.
- The progress bar pulses through the theme's gradient palette every
  second — eye candy with no extra CPU cost beyond the existing repaint.

Fallback: if `rich` isn't importable, `LiveImportUI` is replaced by
`PlainImportUI` which just prints a one-line summary every N files.
The interface is identical so the importer doesn't care which it has.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

# Rich is required for the live UI. We treat its absence as a soft fallback.
try:
    from rich.align import Align
    from rich.console import Console, Group
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.progress_bar import ProgressBar
    from rich.table import Table
    from rich.text import Text
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False


# =============================================================================
# THEMES
# =============================================================================

@dataclass(frozen=True)
class Theme:
    """
    A colour palette. All values are Rich style strings.

    `gradient` is a list of colours that animations cycle through —
    progress bar pulse, title-bar wave, etc. Keep it 4-8 entries with
    smooth transitions between adjacent colours.
    """
    name: str
    border: str
    border_title: str
    accent: str           # primary highlight (progress bar fill, important values)
    dim: str              # subdued text (paths, secondary info)
    text: str             # main text
    label: str            # tag-key labels ("artist:", "label:")
    success: str
    warning: str
    error: str
    duplicate: str
    gradient: list[str]   # for animations
    spinner_chars: str = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"   # braille spinner
    # Optional background colour — when set, the menu and import panels
    # paint this colour behind drawn cells using Rich's "X on Y" syntax.
    # None means "don't touch the background" (terminal default wins).
    # Useful for themes like '4chan' where the cream background IS the look.
    # Caveat: the cell-painting only covers where Rich actually draws.
    # If your terminal window is wider than the menu, the unpainted area
    # stays your terminal's default colour. Resize to match for the full effect.
    background: str | None = None


# =============================================================================
# THEMES
# =============================================================================
#
# A Theme has fixed-role colours (border, text, label, etc) plus a `gradient`
# palette used for animations. Two patterns work well:
#
#  1. *Diverging* gradient — strong contrast, alternates between extremes.
#     Good for energetic / synthwave / fire themes.
#  2. *Smooth* gradient — small steps between adjacent colours. Good for
#     calm / monochrome / single-character themes.
#
# Rich style strings accept: named colours ('bright_cyan'), 256-colour
# names ('grey50'), or hex ('#ff00ff'). Hex is best for themes you
# care about because it's truecolor — no rounding to nearest 256.
#
# Adding a new theme:
#   Theme(
#     name="my_theme",
#     border="<rich style>",
#     border_title="<rich style>",
#     accent="<rich style>",      # main highlight
#     dim="<rich style>",
#     text="<rich style>",
#     label="<rich style>",       # tag-key labels
#     success="<rich style>",
#     warning="<rich style>",
#     error="<rich style>",
#     duplicate="<rich style>",
#     gradient=["#......", ...],  # 4-8 hex colours
#   )
# Then add to THEMES dict.


def _t(name, border, accent, gradient, *, dim="grey50", text="white",
       label=None, success="bright_green", warning="bright_yellow",
       error="bright_red", duplicate="cyan", border_title=None,
       background=None):
    """Shorter constructor — most themes don't need all 12 slots customised."""
    return Theme(
        name=name,
        border=border,
        border_title=border_title or f"bold {border}",
        accent=accent,
        dim=dim,
        text=text,
        label=label or border,
        success=success,
        warning=warning,
        error=error,
        duplicate=duplicate,
        gradient=gradient,
        background=background,
    )


THEMES: dict[str, Theme] = {

    # ─── ORIGINAL FIVE ──────────────────────────────────────────────────

    "cyber":     _t("cyber", "bright_cyan", "bright_magenta",
                    ["#ff00ff", "#ff44ff", "#aa00ff", "#5500ff",
                     "#0044ff", "#00aaff", "#00ffff", "#00ffaa"]),
    "matrix":    _t("matrix", "green", "bright_green", text="green",
                    dim="dark_green", duplicate="dark_green",
                    gradient=["#003300", "#005500", "#008800", "#00bb00",
                              "#00ee00", "#33ff33", "#00ee00", "#008800"]),
    "synthwave": _t("synthwave", "magenta", "bright_magenta",
                    text="bright_white", dim="purple4", label="bright_cyan",
                    duplicate="bright_blue",
                    gradient=["#ff006e", "#ff4090", "#c81d77", "#8f1c5f",
                              "#5e1747", "#3a1078", "#7b2cbf", "#c8b6ff"]),
    "mono":      _t("mono", "white", "bright_white", text="white",
                    duplicate="grey70",
                    gradient=["grey30", "grey50", "grey70", "white",
                              "grey70", "grey50", "grey30", "grey15"]),
    "fire":      _t("fire", "bright_red", "bright_yellow",
                    border_title="bold bright_yellow", dim="dark_red",
                    duplicate="orange3",
                    gradient=["#ff0000", "#ff3300", "#ff6600", "#ff9900",
                              "#ffcc00", "#ff9900", "#ff6600", "#ff3300"]),

    # ─── NATURE / SEASONS ───────────────────────────────────────────────

    "ocean":     _t("ocean", "#4a9eff", "#00d4ff", text="#cce7ff",
                    dim="#3a5f7a", label="#4a9eff", duplicate="#2c5282",
                    gradient=["#001a33", "#003366", "#004080", "#0066cc",
                              "#3399ff", "#66ccff", "#99e6ff", "#cce7ff"]),
    "forest":    _t("forest", "#4a7c59", "#9bc53d", text="#dde5b6",
                    dim="#3a5a40", label="#a4ac86", duplicate="#588157",
                    gradient=["#1a2e1a", "#2d4a2d", "#4a7c59", "#588157",
                              "#9bc53d", "#a4ac86", "#dde5b6", "#a4ac86"]),
    "sunset":    _t("sunset", "#ff6b6b", "#feca57",
                    border_title="bold #feca57", dim="#5c3a3a",
                    label="#ff9f43",
                    gradient=["#ff6b6b", "#ff9f43", "#feca57", "#ff9ff3",
                              "#f368e0", "#ee5a6f", "#ff6b6b", "#ff9f43"]),
    "autumn":    _t("autumn", "#d35400", "#f39c12", text="#ecf0f1",
                    dim="#7d3c1a", label="#e67e22",
                    gradient=["#6b2c0c", "#a04000", "#d35400", "#e67e22",
                              "#f39c12", "#f1c40f", "#f39c12", "#e67e22"]),
    "winter":    _t("winter", "#74b9ff", "#dfe6e9", text="#dfe6e9",
                    dim="#636e72", label="#a8e6ff", duplicate="#74b9ff",
                    gradient=["#2d3436", "#636e72", "#74b9ff", "#a8e6ff",
                              "#dfe6e9", "#ffffff", "#dfe6e9", "#a8e6ff"]),
    "spring":    _t("spring", "#6ab04c", "#f0932b", text="#ffeaa7",
                    dim="#2d5016", label="#badc58",
                    gradient=["#6ab04c", "#badc58", "#f9ca24", "#f0932b",
                              "#eb4d4b", "#ff7979", "#fd79a8", "#a29bfe"]),

    # ─── CYBERPUNK & NEON ───────────────────────────────────────────────

    "vaporwave": _t("vaporwave", "#ff71ce", "#01cdfe", text="#b967ff",
                    dim="#5f0a87", label="#fffb96",
                    gradient=["#ff71ce", "#b967ff", "#01cdfe", "#05ffa1",
                              "#fffb96", "#ff71ce", "#b967ff", "#01cdfe"]),
    "tron":      _t("tron", "#00d4ff", "#ffffff", text="#00d4ff",
                    dim="#003d4a", label="#00d4ff",
                    gradient=["#001a1f", "#002c33", "#005566", "#0099b3",
                              "#00d4ff", "#7fe9ff", "#00d4ff", "#0099b3"]),
    "neon_pink": _t("neon_pink", "#ff0080", "#ff66c4", text="#ffcce4",
                    dim="#660033", label="#ff0080",
                    gradient=["#330019", "#660033", "#990050", "#cc0066",
                              "#ff0080", "#ff66c4", "#ffb3e0", "#ffcce4"]),
    "miami":     _t("miami", "#ff6ec7", "#1de9b6", text="#fdfd96",
                    dim="#660066",
                    gradient=["#ff6ec7", "#fc9ddc", "#fdfd96", "#1de9b6",
                              "#00bcd4", "#7c4dff", "#ff6ec7", "#fdfd96"]),
    "hacker":    _t("hacker", "#39ff14", "#39ff14", text="#0aff0a",
                    dim="#003300", label="#39ff14",
                    gradient=["#003300", "#006600", "#00aa00", "#00ff00",
                              "#39ff14", "#7fff7f", "#39ff14", "#00ff00"]),

    # ─── MONOCHROME VARIANTS ────────────────────────────────────────────

    "amber":     _t("amber", "#ffb000", "#ffd700", text="#ffb000",
                    dim="#3d2900", label="#ffb000", duplicate="#cc8c00",
                    gradient=["#1a1100", "#3d2900", "#5e3f00", "#806000",
                              "#ffb000", "#ffd700", "#ffe45c", "#fff2b3"]),
    "phosphor":  _t("phosphor", "#33ff66", "#66ff99", text="#33ff66",
                    dim="#0a3318", label="#33ff66",
                    gradient=["#0a3318", "#155c2d", "#1f8a44", "#33ff66",
                              "#66ff99", "#99ffbb", "#66ff99", "#33ff66"]),
    "newsprint": _t("newsprint", "white", "bright_white", text="grey85",
                    dim="grey50", label="white",
                    gradient=["grey15", "grey30", "grey50", "grey70",
                              "grey85", "white", "grey85", "grey70"]),
    "blueprint": _t("blueprint", "#5dade2", "#aed6f1", text="#aed6f1",
                    dim="#1b4f72", label="#5dade2",
                    gradient=["#0e2f4f", "#1b4f72", "#2874a6", "#3498db",
                              "#5dade2", "#aed6f1", "#d6eaf8", "#aed6f1"]),

    # ─── DARK / GOTHIC ──────────────────────────────────────────────────

    "dracula":   _t("dracula", "#bd93f9", "#ff79c6", text="#f8f8f2",
                    dim="#6272a4", label="#8be9fd", duplicate="#50fa7b",
                    gradient=["#282a36", "#44475a", "#6272a4", "#bd93f9",
                              "#ff79c6", "#ff5555", "#ffb86c", "#f1fa8c"]),
    "nord":      _t("nord", "#88c0d0", "#8fbcbb", text="#eceff4",
                    dim="#4c566a", label="#88c0d0", duplicate="#a3be8c",
                    gradient=["#2e3440", "#3b4252", "#4c566a", "#5e81ac",
                              "#81a1c1", "#88c0d0", "#8fbcbb", "#eceff4"]),
    "solarized": _t("solarized", "#268bd2", "#b58900", text="#93a1a1",
                    dim="#586e75", label="#2aa198", duplicate="#6c71c4",
                    gradient=["#002b36", "#073642", "#586e75", "#657b83",
                              "#839496", "#93a1a1", "#eee8d5", "#fdf6e3"]),
    "gruvbox":   _t("gruvbox", "#fb4934", "#fabd2f", text="#ebdbb2",
                    dim="#665c54", label="#fe8019", duplicate="#83a598",
                    gradient=["#282828", "#cc241d", "#d65d0e", "#d79921",
                              "#98971a", "#458588", "#b16286", "#a89984"]),

    # ─── HIGH-CONTRAST ─────────────────────────────────────────────────
    #
    # Glossy Red (#DE0000) + Full White (#FFFFFF) + Black (#000000) from
    # the RAL/Pantone spec you supplied. Bold, automotive/cinematic feel —
    # think Ferrari, classic horror posters, Coke branding. Single accent
    # colour with stark contrast.

    "crimson":   _t("crimson", "#DE0000", "#DE0000",
                    border_title="bold #FFFFFF",
                    dim="#660000", text="#FFFFFF", label="#DE0000",
                    success="#FFFFFF", warning="#DE0000", error="#FF4040",
                    duplicate="#888888",
                    gradient=["#000000", "#330000", "#660000", "#990000",
                              "#DE0000", "#FFFFFF", "#DE0000", "#990000"]),

    # ─── IMAGEBOARDS ───────────────────────────────────────────────────
    #
    # 4chan's "Yotsuba B" classic style: cream page background, dark-red
    # post text, "Anonymous" green name, greentext on quotes. This is one
    # of the themes that paints its own background — see Theme.background
    # docstring for why that's a UX-specific feature.
    #
    # Colour names from /jp/'s old colour-hex.com palette and the actual
    # CSS of the legacy /b/ board: post text #800000, anonymous #117743,
    # greentext #789922, quote link #FF0000, background #FFFFEE.

    "4chan":     _t("4chan", "#D9BFB7", "#FF0000",
                    border_title="bold #800000",
                    dim="#789922", text="#800000", label="#117743",
                    success="#117743", warning="#FF0000", error="#FF0000",
                    duplicate="#7A983A",
                    gradient=["#117743", "#7A983A", "#789922", "#0F0C5D",
                              "#800000", "#FF0000", "#800000", "#789922"],
                    background="#FFFFEE"),

    # ─── MEMES ─────────────────────────────────────────────────────────

    # Pepe the Frog — Matt Furie's original 2005 character.
    # Greens of his skin + pink lip + brown vest. Wholesome zen origin
    # version (Boy's Club), not the politicised crusade fork.
    "pepe":      _t("pepe", "#558B2F", "#7CB342", text="#dcedc8",
                    border_title="bold #7CB342",
                    dim="#33691e", label="#aed581",
                    duplicate="#8d6e63",
                    gradient=["#33691e", "#558b2f", "#7cb342", "#aed581",
                              "#f06292", "#8d6e63", "#558b2f", "#7cb342"]),

    # Doge — Shiba Inu yellow/cream coat + Comic-Sans-era multicoloured text.
    "doge":      _t("doge", "#FFC107", "#FFEB3B",
                    border_title="bold #FFC107",
                    dim="#8d6e63", text="#fff8e1", label="#FFEB3B",
                    duplicate="#a1887f",
                    gradient=["#ef5350", "#ffeb3b", "#42a5f5", "#66bb6a",
                              "#ab47bc", "#ffeb3b", "#ef5350", "#66bb6a"]),

    # Gigachad — monochrome chiselled-jaw photo filter aesthetic.
    "gigachad":  _t("gigachad", "#bdbdbd", "#ffffff",
                    border_title="bold #ffffff",
                    dim="#424242", text="#e0e0e0", label="#bdbdbd",
                    duplicate="#757575",
                    gradient=["#000000", "#212121", "#424242", "#616161",
                              "#9e9e9e", "#bdbdbd", "#e0e0e0", "#ffffff"]),

    # Troll face — classic 2008 black-on-white MS Paint.
    "trollface": _t("trollface", "#000000", "#000000",
                    border_title="bold #000000",
                    dim="#888888", text="#000000", label="#000000",
                    success="#000000", warning="#000000", error="#FF0000",
                    duplicate="#888888",
                    gradient=["#ffffff", "#dddddd", "#aaaaaa", "#888888",
                              "#444444", "#000000", "#444444", "#888888"],
                    background="#ffffff"),

    # Nyan Cat — pink poptart body, grey cat, rainbow trail.
    "nyancat":   _t("nyancat", "#FF69B4", "#FFFFFF",
                    border_title="bold #FF69B4",
                    dim="#777777", text="#FFFFFF", label="#FFC0CB",
                    duplicate="#999999",
                    gradient=["#FF0000", "#FF8800", "#FFFF00", "#00FF00",
                              "#0088FF", "#8800FF", "#FF69B4", "#FFFFFF"]),

    # Stonks — green-up red-down stock-meme aesthetic on dark bg.
    "stonks":    _t("stonks", "#26a69a", "#26a69a",
                    border_title="bold #26a69a",
                    dim="#37474f", text="#eceff1", label="#26a69a",
                    success="#26a69a", warning="#ffa726", error="#ef5350",
                    duplicate="#546e7a",
                    gradient=["#ef5350", "#ff7043", "#ffa726", "#ffee58",
                              "#9ccc65", "#66bb6a", "#26a69a", "#42a5f5"]),

    # Catjam — orange tabby cat bobbing to music.
    "catjam":    _t("catjam", "#FF8C42", "#FFB877",
                    border_title="bold #FF8C42",
                    dim="#5d4037", text="#FFE0B2", label="#FFB877",
                    duplicate="#8d6e63",
                    gradient=["#5d4037", "#795548", "#8d6e63", "#bf6334",
                              "#FF8C42", "#FFB877", "#FFE0B2", "#FFB877"]),

    # Bonk — cartoon yellow with brown outline, dog bonking another dog.
    "bonk":      _t("bonk", "#F9A825", "#FFD54F",
                    border_title="bold #F9A825",
                    dim="#5d4037", text="#FFF59D", label="#FFD54F",
                    duplicate="#8d6e63",
                    gradient=["#5d4037", "#795548", "#a1887f", "#F9A825",
                              "#FFD54F", "#FFF59D", "#FFD54F", "#F9A825"]),

    # ─── MLP — MY LITTLE PONY ───────────────────────────────────────────
    #
    # Character palettes taken from canonical colour specs:
    # coat outline -> border, mane fills -> gradient, accent mane stripe -> accent.
    # The point is that someone bored watching a 12 TB import gets to
    # stare at a coherent character palette. Choose your favourite.

    "rainbowdash":     _t(
        # Sky-blue coat with the full rainbow mane.
        "rainbowdash", "#1B98D1", "#EC4141",
        border_title="bold #1B98D1",
        dim="#5C96C9", text="#9BDBF5", label="#6BABDA",
        gradient=[
            "#EC4141",  # red    (mane Fill 2)
            "#EF7135",  # orange (mane Fill 3)
            "#FAF5AB",  # yellow (mane Fill 4)
            "#5FBB4E",  # green  (mane Fill 5)
            "#1B98D1",  # blue   (mane Fill 1)
            "#632E86",  # purple (mane Fill 6)
            "#1B98D1",  # blue (loop back smoothly)
            "#5FBB4E",  # green
        ],
    ),

    "twilightsparkle": _t(
        # Purple coat, dark-blue mane with a hot-pink stripe.
        "twilightsparkle", "#A46BBD", "#EA428B",
        border_title="bold #A46BBD",
        dim="#9156A9", text="#CC9CDF", label="#BF89D1",
        gradient=[
            "#132248", "#243870", "#652D87", "#A46BBD",
            "#CC9CDF", "#EA428B", "#A46BBD", "#652D87",
        ],
    ),

    "applejack":       _t(
        # Orange coat, blonde mane, country brown hat.
        "applejack", "#EF6F2F", "#EC3F41",
        border_title="bold #EF6F2F",
        dim="#B2884D", text="#FABA62", label="#E7D462",
        gradient=[
            "#B2884D",  # hat
            "#CA9A56",  # hat fill
            "#EF6F2F",  # coat
            "#FABA62",  # coat fill
            "#FAF5AB",  # mane
            "#E7D462",  # mane outline
            "#EC3F41",  # apple cutie-mark
            "#6BB944",  # leaves
        ],
    ),

    "fluttershy":      _t(
        # Soft yellow coat, pink mane, very pastel.
        "fluttershy", "#E9D461", "#E581B1",
        border_title="bold #E581B1",
        dim="#F3E488", text="#FAF5AB", label="#F3B5CF",
        gradient=[
            "#FAF5AB", "#F3E488", "#E9D461", "#F3B5CF",
            "#E581B1", "#69C8C3", "#84D2D4", "#F3B5CF",
        ],
    ),

    "pinkiepie":       _t(
        # Pink everything. Bright and bubbly.
        "pinkiepie", "#E880B0", "#EB458B",
        border_title="bold #EB458B",
        dim="#DD6FA4", text="#F5B7D0", label="#7ED0F2",
        gradient=[
            "#BB1C76", "#EB458B", "#E880B0", "#F5B7D0",
            "#FAF5AB", "#7ED0F2", "#F5B7D0", "#EB458B",
        ],
    ),

    "rarity":          _t(
        # White coat, purple mane, sapphire-blue eyes.
        "rarity", "#BDC1C2", "#4A1767",
        border_title="bold #4A1767",
        dim="#82C1DC", text="#EAEEF0", label="#5E50A0",
        gradient=[
            "#EAEEF0", "#BDC1C2", "#794897", "#4A1767",
            "#5E50A0", "#3977B8", "#5693CF", "#B8E1F0",
        ],
    ),

    "spike":           _t(
        # Purple dragon, green spikes.
        "spike", "#985E9F", "#50C356",
        border_title="bold #50C356",
        dim="#AF72B6", text="#C290C6", label="#AFD95E",
        gradient=[
            "#985E9F", "#C290C6", "#AF72B6", "#2E992E",
            "#50C356", "#AFD95E", "#DCF188", "#96CE7D",
        ],
    ),
}


def get_theme(name: str, *, prefer: str | None = None) -> Theme:
    """
    Look up a theme by name, falling back to 'cyber' if unknown.

    prefer:
      - 'external' (default behaviour when None): try the user's theme
        file first, fall back to the built-in.
      - 'native': skip user files, use only built-ins.
      - None: read the user's saved preference from config; if absent,
        defaults to 'external'.

    If a file-based theme fails to load (malformed TOML, missing
    fields), we fall back to the built-in silently — the built-in
    registry is the safety net.
    """
    nm = name.lower() if isinstance(name, str) else name
    # Resolve `prefer` default — read config when prefer=None
    if prefer is None:
        try:
            from config import load_config
            cfg = load_config()
            prefer = cfg.get("ui", {}).get("theme_source", "external")
        except Exception:
            prefer = "external"

    if prefer == "external":
        try:
            import themes as _themes_mod
            try:
                return _themes_mod.get_theme(nm, prefer="external")
            except KeyError:
                pass
            except Exception:
                # Any unexpected failure in themes.py — fall through
                # to the built-in registry. The whole point of keeping
                # the natives embedded is so a broken external file
                # can never lock the user out.
                pass
        except ImportError:
            # themes.py not installed — fine, just use built-ins
            pass
    # Built-in fallback
    return THEMES.get(nm, THEMES["cyber"])


# =============================================================================
# STATE
# =============================================================================

@dataclass
class ImportState:
    """
    Shared state between the importer (writer) and the UI (reader).

    All mutations go through `LiveImportUI.update()` which takes a lock,
    so the importer can call it from any thread.
    """
    # Current work
    current_folder: str = ""
    current_file: str = ""
    file_index_in_folder: int = 0
    files_in_folder: int = 0
    file_size_bytes: int = 0
    file_codec: str = ""
    file_format_detail: str = ""    # e.g. "16-bit / 44.1 kHz"

    # What's being extracted (tag dict)
    grabbing: dict[str, str] = field(default_factory=dict)

    # Where it's going
    organising_to: str = ""

    # Counters (totals across the whole run)
    files_done: int = 0
    files_total: int = 0
    files_imported: int = 0
    files_duplicate: int = 0
    files_broken: int = 0
    bytes_processed: int = 0
    # Unit label for the progress bar's rate display. When the
    # operation is fetching metadata one-album-at-a-time, "files"
    # is misleading — set this to "albums" so the user sees "1.0
    # albums/s" instead of "1.0 files/s".
    unit_label: str = "files"

    # Timing
    started_at: float = 0.0

    # Recent activity log (most recent first)
    activity: list[tuple[float, str, str]] = field(default_factory=list)
    # tuples are (timestamp, kind, message); kind in {imported,duplicate,broken,info}

    # Title (what mode are we in: "Import", "Index", etc.)
    mode_label: str = "Import"


# =============================================================================
# HELPERS
# =============================================================================

def _fmt_size(n: int) -> str:
    if n <= 0:
        return "0 B"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.1f} {u}" if u != "B" else f"{int(f)} B"
        f /= 1024
    return f"{f:.1f} TiB"


def _fmt_duration(seconds: float) -> str:
    if seconds is None or seconds < 0 or seconds != seconds:  # negative or NaN
        return "--:--"
    if seconds == float("inf"):
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _marquee(text: str, width: int, frame: int, gap: int = 4) -> str:
    """
    Scroll `text` horizontally so all of it gets shown across frames.

    If `text` fits in `width`, returns it left-padded with nothing.
    Otherwise treats it as a loop: "<text>    <text>    " and slides
    the viewport.
    """
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    loop = text + " " * gap
    pos = frame % len(loop)
    # Get `width` chars starting at pos, wrapping around.
    if pos + width <= len(loop):
        return loop[pos:pos + width]
    overshoot = (pos + width) - len(loop)
    return loop[pos:] + loop[:overshoot]


# =============================================================================
# LIVE UI
# =============================================================================

class LiveImportUI:
    """
    Wraps a Rich `Live` session showing the multi-panel layout.

    Usage:
        ui = LiveImportUI(theme="cyber", mode_label="Import")
        ui.set_total(2000)
        with ui:
            for file in stuff:
                ui.update(current_file=str(file), ...)
                # ... do work ...
                ui.advance(imported=True)

    The class is thread-safe — update/advance/log can be called from
    any thread. The render loop runs in Rich's own thread.
    """

    def __init__(
        self,
        *,
        theme: str = "cyber",
        mode_label: str = "Import",
        refresh_per_second: int = 20,
        activity_max: int | None = None,
        force_terminal: bool = False,
        color_system: str = "auto",
    ):
        if not RICH_AVAILABLE:
            raise RuntimeError("rich is required for LiveImportUI")

        self.theme = get_theme(theme)
        self.state = ImportState(mode_label=mode_label)
        self.state.started_at = time.time()

        self._lock = threading.Lock()
        # Activity buffer is a flat list capped at a constant high value
        # (not tied to terminal height). Rich's Layout naturally clips
        # the activity panel to fit whatever vertical space the terminal
        # gives it on each render, so we just need to keep enough rows
        # in memory to fill the panel even on very tall terminals.
        #
        # Why this isn't dynamic: previously we set _activity_max from
        # os.get_terminal_size() at __init__ time. That snapshot didn't
        # update on resize — so when the user enlarged the terminal,
        # the panel grew (Rich did its job) but we'd only have 30 rows
        # in memory and the bottom of the panel was empty. Keeping a
        # generous fixed buffer side-steps the whole problem.
        #
        # 200 entries × ~80 chars each ≈ 16 KB — tiny memory cost for
        # a smooth resize experience.
        if activity_max is None:
            activity_max = 200
        self._activity_max = activity_max
        self._frame = 0
        self._refresh_per_second = refresh_per_second

        # ----- EMA-smoothed rate state -----
        # The progress-bar ETA needs a "recent" rate, not the cumulative
        # average. Cumulative drifts: early cache-hit albums make the
        # rate look fast, then the network-rate-limited middle of the
        # run drags it down. The UI's ETA goes UP as the run proceeds,
        # which is the user's complaint.
        #
        # Fix: exponentially-weighted moving average over a ~60-second
        # window. Sample every N seconds; each sample's weight decays
        # over time. Smoother than a sliding window, less state to
        # carry. The half-life α is chosen so a sample dominates for
        # ~30 seconds before older samples take over.
        #
        # Constants:
        #   _RATE_SAMPLE_INTERVAL = how often we capture (completed,
        #                            time) and update the EMA
        #   _RATE_EMA_ALPHA       = 1 - exp(-Δt / τ) for τ = 30s
        self._rate_ema: float | None = None
        self._rate_last_sample_at: float = 0.0
        self._rate_last_completed: int = 0
        self._RATE_SAMPLE_INTERVAL = 2.0   # seconds between samples
        self._RATE_TAU = 30.0              # smoothing time constant

        # Honour terminal-detection overrides from config.
        console_kwargs: dict[str, Any] = {}
        if force_terminal:
            console_kwargs["force_terminal"] = True
        if color_system != "auto":
            console_kwargs["color_system"] = color_system
        self.console = Console(**console_kwargs)
        self._live: Live | None = None

    # -- context manager -------------------------------------------------

    def __enter__(self) -> "LiveImportUI":
        self._live = Live(
            self._render(),
            console=self.console,
            refresh_per_second=self._refresh_per_second,
            screen=False,         # don't take over the alternate screen
            transient=False,      # leave the final frame in scrollback
            redirect_stdout=True,
            redirect_stderr=True,
            # Rich defaults vertical_overflow="ellipsis" which clips
            # content at its initially-detected height. When the user
            # enlarges the terminal mid-run, Rich keeps drawing at the
            # OLD height — activity panel doesn't grow even though the
            # Layout has computed more space for it. "visible" tells
            # Rich to draw the full layout each frame regardless of
            # what it drew last time.
            vertical_overflow="visible",
        )
        self._live.__enter__()
        # Track the last terminal size we rendered against. The render
        # loop compares this every frame and forces a full re-init when
        # it changes — Rich's Live with screen=False doesn't naturally
        # detect SIGWINCH growth (it knows shrink because it can't draw
        # past the bottom, but doesn't reclaim newly available rows).
        try:
            ts = os.get_terminal_size()
            self._last_size = (ts.columns, ts.lines)
        except Exception:
            self._last_size = (80, 24)
        # Drive repaints from a side thread so animations keep moving
        # even when the importer is between update() calls.
        self._stop = threading.Event()
        self._render_thread = threading.Thread(
            target=self._render_loop, name="ui-render", daemon=True
        )
        self._render_thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        if hasattr(self, "_render_thread"):
            self._render_thread.join(timeout=1.0)
        if self._live is not None:
            self._live.__exit__(exc_type, exc, tb)
            self._live = None

    def _render_loop(self) -> None:
        interval = 1.0 / max(1, self._refresh_per_second)
        while not self._stop.is_set():
            self._frame += 1
            try:
                # Detect terminal-size changes (SIGWINCH). Rich's Live
                # with screen=False doesn't naturally reclaim newly-
                # available rows on terminal grow. We force-refresh on
                # any size change so the layout reflows. Calling
                # console._refresh isn't a public API but works across
                # Rich versions; if it ever breaks, fall back to
                # stopping+restarting the Live display.
                try:
                    ts = os.get_terminal_size()
                    current = (ts.columns, ts.lines)
                    if current != self._last_size:
                        self._last_size = current
                        # Console.size is a property that re-reads each
                        # access; clearing any cached width/height hint
                        # makes the next render see the new dimensions.
                        # Rich's Console doesn't actually cache these
                        # internally — it's the Live display that
                        # locked-in the original height. Push it.
                        if self._live is not None:
                            self._live.refresh()
                except OSError:
                    # No controlling terminal (e.g. piped output) —
                    # not resizable anyway, silently skip.
                    pass
                if self._live is not None:
                    self._live.update(self._render(), refresh=True)
            except Exception:
                # Don't crash the import because of a render glitch.
                pass
            time.sleep(interval)

    # -- public API ------------------------------------------------------

    def update(self, **kwargs: Any) -> None:
        """Replace any number of state fields atomically."""
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self.state, k):
                    setattr(self.state, k, v)

    def set_grabbing(self, grabbing: dict[str, Any] | None) -> None:
        """Replace the 'grabbing' dict (tag values being extracted)."""
        with self._lock:
            if grabbing is None:
                self.state.grabbing = {}
            else:
                # Only show the most interesting fields, in this order.
                # Anything else stays in tags_raw and isn't shown here.
                interesting = (
                    "artist", "albumartist", "album", "title",
                    "label", "year", "track_number", "codec",
                )
                self.state.grabbing = {
                    k: str(grabbing[k])
                    for k in interesting
                    if grabbing.get(k)
                }

    def set_total(self, total: int) -> None:
        with self._lock:
            self.state.files_total = total

    def set_unit(self, unit: str) -> None:
        """Override the unit label shown in the progress bar.
        Default is 'files'; metadata fetch should pass 'albums'."""
        with self._lock:
            self.state.unit_label = unit or "files"

    def advance(
        self,
        *,
        imported: bool = False,
        duplicate: bool = False,
        broken: bool = False,
        size_bytes: int = 0,
    ) -> None:
        """Increment the right counters after a file is processed."""
        with self._lock:
            self.state.files_done += 1
            self.state.bytes_processed += max(0, size_bytes)
            if imported:
                self.state.files_imported += 1
            if duplicate:
                self.state.files_duplicate += 1
            if broken:
                self.state.files_broken += 1

    def log(self, kind: str, message: str) -> None:
        """Append a line to the activity log."""
        with self._lock:
            self.state.activity.insert(0, (time.time(), kind, message))
            if len(self.state.activity) > self._activity_max:
                self.state.activity = self.state.activity[:self._activity_max]

    # -- render ----------------------------------------------------------

    def _render(self):
        with self._lock:
            s = self._snapshot_state()

        theme = self.theme
        layout = Layout(name="root")

        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="folder_file", size=5),
            Layout(name="grab_dest", size=12),
            Layout(name="progress", size=4),
            Layout(name="activity"),   # takes remaining space
        )
        layout["folder_file"].split_row(
            Layout(name="folder"),
            Layout(name="file"),
        )
        layout["grab_dest"].split_row(
            Layout(name="grabbing"),
            Layout(name="destination"),
        )

        layout["header"].update(self._render_header(s))
        layout["folder"].update(self._render_folder_panel(s))
        layout["file"].update(self._render_file_panel(s))
        layout["grabbing"].update(self._render_grabbing_panel(s))
        layout["destination"].update(self._render_destination_panel(s))
        layout["progress"].update(self._render_progress_panel(s))
        layout["activity"].update(self._render_activity_panel(s))

        return layout

    def _snapshot_state(self) -> ImportState:
        # Copy mutable fields so the renderer doesn't see torn writes
        # if an updater happens to run during render.
        s = self.state
        return ImportState(
            current_folder=s.current_folder,
            current_file=s.current_file,
            file_index_in_folder=s.file_index_in_folder,
            files_in_folder=s.files_in_folder,
            file_size_bytes=s.file_size_bytes,
            file_codec=s.file_codec,
            file_format_detail=s.file_format_detail,
            grabbing=dict(s.grabbing),
            organising_to=s.organising_to,
            files_done=s.files_done,
            files_total=s.files_total,
            files_imported=s.files_imported,
            files_duplicate=s.files_duplicate,
            files_broken=s.files_broken,
            bytes_processed=s.bytes_processed,
            started_at=s.started_at,
            activity=list(s.activity),
            mode_label=s.mode_label,
        )

    # -- panels ---------------------------------------------------------

    def _spinner_char(self) -> str:
        return self.theme.spinner_chars[self._frame % len(self.theme.spinner_chars)]

    def _gradient_pick(self, offset: int = 0) -> str:
        """Pick a gradient colour for the current frame + offset."""
        palette = self.theme.gradient
        return palette[(self._frame + offset) % len(palette)]

    def _render_header(self, s: ImportState):
        # Leading icon — music note in the corner. Nerd Font when
        # available, ASCII fallback otherwise.
        try:
            from icons import icon as _icon, use_nerd_font
            lead = _icon("music")
            clock_icon = _icon("clock", fallback="◷")
        except Exception:
            lead = "♪"
            clock_icon = "◷"

        # Animated title bar: each character gets a colour from the
        # gradient palette, with an offset that shifts each frame so
        # the wave moves left-to-right.
        title = f"  {lead}  music-organiser · {s.mode_label}  "
        text = Text()
        for i, ch in enumerate(title):
            colour = self.theme.gradient[
                (self._frame + i) % len(self.theme.gradient)
            ]
            text.append(ch, style=colour)

        # Right-side meta: theme name, animated clock + elapsed, wall clock.
        # "Animated" here means the clock icon's COLOUR pulses through the
        # theme gradient. The glyph itself stays still — we tried cycling
        # 12 hour-specific clock glyphs but those codepoints don't render
        # reliably across Nerd Font versions (NF v3 moved some MDI
        # codepoints), so single glyph + colour pulse is the safer trick.
        elapsed = time.time() - s.started_at if s.started_at else 0
        is_running = (s.files_done > 0 and s.files_done < s.files_total)

        # Build the meta as a Text so the clock icon can be coloured
        # independently of the rest.
        meta = Text()
        meta.append(f"theme: {self.theme.name}   ", style=self.theme.dim)
        if is_running:
            # Pulse the clock through the gradient
            pulse_colour = self.theme.gradient[
                self._frame % len(self.theme.gradient)
            ]
            meta.append(clock_icon, style=f"bold {pulse_colour}")
        else:
            # Idle — dim static
            meta.append(clock_icon, style=self.theme.dim)
        meta.append(f"  elapsed: {_fmt_duration(elapsed)}",
                     style=self.theme.dim)

        # Two-column header (title left, meta right)
        tbl = Table.grid(expand=True)
        tbl.add_column(justify="left", ratio=2)
        tbl.add_column(justify="right", ratio=1)
        tbl.add_row(text, meta)

        return Panel(
            tbl,
            border_style=self.theme.border,
            padding=(0, 1),
            title=None,
        )

    def _render_folder_panel(self, s: ImportState):
        spinner = self._spinner_char() if s.current_folder else " "
        # Folder line + the path beneath
        folder_short = os.path.basename(s.current_folder.rstrip(os.sep)) or "—"
        path_full = s.current_folder or "—"

        width = max(20, self.console.width // 2 - 6)
        scroll_path = _marquee(path_full, width, self._frame // 3)

        body = Group(
            Text.assemble(
                (f"{spinner} ", self.theme.accent),
                (folder_short, f"bold {self.theme.text}"),
            ),
            Text(scroll_path, style=self.theme.dim, overflow="ellipsis", no_wrap=True),
        )
        return Panel(
            body,
            title="[" + self.theme.border_title + "]Folder[/]",
            border_style=self.theme.border,
            padding=(0, 1),
        )

    def _render_file_panel(self, s: ImportState):
        spinner = self._spinner_char() if s.current_file else " "
        filename = os.path.basename(s.current_file) or "—"
        # Index
        if s.files_in_folder:
            position = f"({s.file_index_in_folder} of {s.files_in_folder})"
        else:
            position = ""

        # Size + codec line
        bits = []
        if s.file_size_bytes:
            bits.append(_fmt_size(s.file_size_bytes))
        if s.file_codec:
            bits.append(s.file_codec)
        if s.file_format_detail:
            bits.append(s.file_format_detail)
        detail = " · ".join(bits) if bits else ""

        width = max(20, self.console.width // 2 - 6)
        scroll_filename = _marquee(filename, width - len(position) - 1, self._frame // 3)

        body = Group(
            Text.assemble(
                (f"{spinner} ", self.theme.accent),
                (scroll_filename, f"bold {self.theme.text}"),
                ("  " + position, self.theme.dim) if position else ("", self.theme.dim),
            ),
            Text(detail, style=self.theme.dim, overflow="ellipsis", no_wrap=True),
        )
        return Panel(
            body,
            title="[" + self.theme.border_title + "]File[/]",
            border_style=self.theme.border,
            padding=(0, 1),
        )

    def _render_grabbing_panel(self, s: ImportState):
        if not s.grabbing:
            body: Any = Align.center(
                Text("(waiting for next file…)", style=self.theme.dim),
                vertical="middle",
            )
        else:
            tbl = Table.grid(padding=(0, 1), expand=True)
            tbl.add_column(justify="right", style=self.theme.label, no_wrap=True)
            tbl.add_column(justify="left", style=self.theme.text, overflow="ellipsis", no_wrap=True)
            for k, v in s.grabbing.items():
                tbl.add_row(f"{k}:", v)
            body = tbl

        return Panel(
            body,
            title="[" + self.theme.border_title + "]Grabbing[/]",
            border_style=self.theme.border,
            padding=(0, 1),
        )

    def _render_destination_panel(self, s: ImportState):
        path = s.organising_to
        if not path:
            body: Any = Align.center(
                Text("(no destination yet)", style=self.theme.dim),
                vertical="middle",
            )
        else:
            # Break the path into stacked, increasingly-indented lines —
            # makes a deep path readable inside a narrow panel.
            #
            # For very deep paths (e.g. /run/media/anon/Expansion/...), the
            # first N parts are noise. Show only the last MAX components,
            # prefixing an ellipsis line if we trimmed.
            MAX_PARTS = 6
            parts = [p for p in path.split(os.sep) if p]
            trimmed = len(parts) > MAX_PARTS
            shown = parts[-MAX_PARTS:] if trimmed else parts

            lines: list[Text] = []
            if trimmed:
                # Show the head as a compact prefix so you know roughly where.
                head = os.sep.join(parts[:len(parts) - MAX_PARTS])
                lines.append(Text(f"…/{head}/", style=self.theme.dim,
                                  overflow="ellipsis", no_wrap=True))
            for i, part in enumerate(shown):
                indent = "  " * min(i, 4)
                is_last = (i == len(shown) - 1)
                arrow = "→ " if is_last else ""
                style = self.theme.accent if is_last else self.theme.dim
                lines.append(Text(f"{indent}{arrow}{part}", style=style,
                                  overflow="ellipsis", no_wrap=True))
            body = Group(*lines)

        return Panel(
            body,
            title="[" + self.theme.border_title + "]Organising to[/]",
            border_style=self.theme.border,
            padding=(0, 1),
        )

    def _render_progress_panel(self, s: ImportState):
        # Bar
        completed = s.files_done
        total = max(s.files_total, completed) or 1
        pct = (completed / total) * 100

        # Full-width gradient bar with rounded caps.
        #
        # Visual design:
        #   ╭━━━━━━━━━━━━━━━━━●·········································╮
        #   the bar SPANS the entire panel width minus the caps. Filled
        #   characters get a gradient colour so the fill sweeps the
        #   theme's rainbow (in REVERSE — so the newest colour is at the
        #   leading dot, matching the way "music-organiser" fades in the
        #   header but inverted in direction).
        try:
            from icons import icon as _icon, use_nerd_font
            dot_glyph = _icon("dot", fallback="●")
            unfilled_glyph = "·"
            filled_glyph = "━"
        except Exception:
            dot_glyph = "●"
            unfilled_glyph = "·"
            filled_glyph = "━"

        # Calculate bar width from console — full panel inner width.
        # Panel border + padding eats 4 cells (1 border each side, 1
        # padding each side). No internal caps now — the panel's own
        # rounded border IS the container the user wants.
        try:
            full_w = self.console.width
        except Exception:
            full_w = 80
        bar_width = max(20, full_w - 6)

        filled_count = int((completed / total) * bar_width) if total else 0
        filled_count = min(filled_count, bar_width)

        # Build the bar. The filled section uses a REVERSE-direction
        # rainbow: gradient[0] sits at the LEADING edge (right end of
        # the fill), so as progress advances, the latest colour sweeps
        # ahead. The title-bar gradient sweeps left→right; this is the
        # mirror so the eye treats them as complementary motions.
        bar = Text("")

        if filled_count > 0:
            grad = self.theme.gradient
            grad_len = max(1, len(grad))
            for i in range(filled_count):
                # The title-bar gradient in _render_header uses
                # `(frame + i) % N` so colours move right→left over time.
                # We want this bar to move left→right (mirror direction)
                # at the same speed, with the same colours. That means
                # negate the frame offset: `(filled_count-1-i - frame)`.
                # Verify: at frame=0, leftmost cell gets grad[fc-1] and
                # rightmost gets grad[0]. At frame=+1, leftmost gets
                # grad[fc-2] and rightmost gets grad[-1]=grad[N-1] — so
                # colours have shifted one step to the LEFT, which is
                # the right→left direction we want from the eye's
                # perspective (colours appear to FLOW leftward… wait).
                #
                # Actually for a *mirror* of right→left, we want left→
                # right flow. The title bar uses (frame+i): at frame+1
                # each char gets what its right neighbour had at frame=0,
                # so colours appear to move LEFTWARD over time
                # (right→left). To mirror, we want (frame-i): each char
                # gets what its LEFT neighbour had at frame=0, so
                # colours flow RIGHTWARD (left→right).
                # In our reversed-base scheme that becomes:
                #   colour_idx = (-(filled_count-1-i) - frame) % N
                #              = (i - filled_count + 1 - frame) % N
                colour_idx = (i - (filled_count - 1) - self._frame) % grad_len
                bar.append(filled_glyph, style=grad[colour_idx])

        if 0 < filled_count < bar_width:
            # Leading dot — bold, on the colour that would be next in
            # the sweep (i.e. position i=filled_count if the fill
            # continued). Keeps the dot visually continuous with the
            # gradient as the bar advances.
            grad = self.theme.gradient
            lead_idx = (filled_count - (filled_count - 1) - self._frame) % max(1, len(grad))
            lead_colour = grad[lead_idx]
            bar.append(dot_glyph, style=f"bold {lead_colour}")
            remaining_unfilled = bar_width - filled_count - 1
            if remaining_unfilled > 0:
                bar.append(unfilled_glyph * remaining_unfilled,
                            style=self.theme.dim)
        elif filled_count == 0:
            bar.append(unfilled_glyph * bar_width, style=self.theme.dim)
        # filled_count == bar_width → no dot, all filled

        # Numeric lines beneath the bar. We add a clock icon to the
        # ETA portion to match the elapsed icon in the header.
        elapsed = max(0.001, time.time() - s.started_at)

        # ----- EMA-smoothed rate -----
        # Cumulative rate (`completed / elapsed`) drifts and produces a
        # constantly-changing ETA. Instead, sample throughput in
        # discrete buckets and feed each bucket into an EMA.
        #
        # On the first ~30 seconds of the run there's not enough data
        # for the EMA to stabilise — we fall back to cumulative rate
        # during that warmup, then switch to EMA. This stops the
        # initial ETA from being wildly wrong while data accumulates.
        now_t = time.time()
        if self._rate_last_sample_at == 0.0:
            # First call — anchor at start.
            self._rate_last_sample_at = s.started_at
            self._rate_last_completed = 0
        dt = now_t - self._rate_last_sample_at
        if dt >= self._RATE_SAMPLE_INTERVAL:
            # Capture a sample: how many completions per second
            # over this interval. Bucketing samples (rather than
            # measuring instantaneously) smooths out the per-frame
            # jitter from variable provider response times.
            delta_completed = completed - self._rate_last_completed
            sample_rate = delta_completed / dt if dt > 0 else 0.0
            if self._rate_ema is None:
                # First sample seeds the EMA directly. Otherwise the
                # first 30 seconds of EMA would start from 0 and drag
                # the ETA toward infinity.
                self._rate_ema = sample_rate
            else:
                # Exponential weight: a sample's influence decays
                # over τ seconds. alpha = 1 - exp(-dt/τ).
                import math
                alpha = 1.0 - math.exp(-dt / self._RATE_TAU)
                self._rate_ema = (alpha * sample_rate
                                  + (1.0 - alpha) * self._rate_ema)
            self._rate_last_sample_at = now_t
            self._rate_last_completed = completed

        # Display rate: EMA once we have it, cumulative during warmup.
        # Warmup is "elapsed < τ" — the first half-life of EMA samples.
        if self._rate_ema is not None and elapsed >= self._RATE_TAU:
            display_rate = self._rate_ema
        else:
            display_rate = completed / elapsed if elapsed > 0 else 0.0

        # ETA: use the smoothed rate. Cap ETA display at 99 hours
        # (anything longer is "you should rethink this run anyway")
        # to avoid the "ETA 4316:00:00" string blowing out the panel.
        if display_rate > 0:
            remaining = (total - completed) / display_rate
            if remaining > 99 * 3600:
                eta_str = ">99h"
            else:
                eta_str = _fmt_duration(remaining)
        else:
            eta_str = "—"
        # Expose display_rate as `rate` for the rest of the renderer
        rate = display_rate

        try:
            from icons import icon as _icon
            eta_icon = _icon("clock", fallback="◷")
        except Exception:
            eta_icon = "◷"

        # Rate display: pick units that show meaningful precision.
        # At ~1 album/s, "1.0 albums/s" loses resolution; switch to
        # albums/min below 5/s so the user sees movement.
        unit = s.unit_label or "files"
        if rate >= 5.0:
            rate_str = f"{rate:.1f} {unit}/s"
        elif rate >= 0.1:
            rate_str = f"{rate * 60:.0f} {unit}/min"
        elif rate > 0:
            rate_str = f"{rate * 3600:.0f} {unit}/hr"
        else:
            rate_str = "warming up…"

        line1 = Text.assemble(
            (f" {pct:5.1f}%  ", f"bold {self.theme.accent}"),
            (f"{completed:,} / {total:,}  ", self.theme.text),
            ("·  ", self.theme.dim),
            (f"{rate_str}  ", self.theme.text),
            ("·  ", self.theme.dim),
            (f"{eta_icon}  ETA {eta_str}", self.theme.text),
            overflow="ellipsis",
            no_wrap=True,
        )
        line2 = Text.assemble(
            (f" imp:{s.files_imported:,}  ", self.theme.text),
            (f"dup:{s.files_duplicate:,}  ", self.theme.text),
            (f"bad:{s.files_broken:,}  ", self.theme.text),
            ("·  ", self.theme.dim),
            (f"{_fmt_size(s.bytes_processed)} processed", self.theme.dim),
            overflow="ellipsis",
            no_wrap=True,
        )

        grid = Table.grid(expand=True)
        grid.add_column()
        grid.add_row(bar)
        grid.add_row(line1)
        grid.add_row(line2)

        return Panel(
            grid,
            title="[" + self.theme.border_title + "]Progress[/]",
            border_style=self.theme.border,
            padding=(0, 1),
        )

    def _render_activity_panel(self, s: ImportState):
        if not s.activity:
            body: Any = Align.center(
                Text("(activity will appear here)", style=self.theme.dim),
                vertical="middle",
            )
        else:
            try:
                from icons import icon as _ic
                kind_icons = {
                    "imported":   _ic("check"),
                    "duplicate":  _ic("file"),
                    "broken":     _ic("cross"),
                    "warning":    _ic("warning"),
                    "info":       _ic("info"),
                }
            except Exception:
                kind_icons = {}
            tbl = Table.grid(padding=(0, 1), expand=True)
            # Timestamp column: always 11 cells (e.g. "12:34:56 pm")
            tbl.add_column(style=self.theme.dim, no_wrap=True, width=11)
            # Kind column: fixed at 11 cells. Longest label is "duplicate"
            # (9) + space + icon (2) = 12; we pad to 11 to leave 1 cell of
            # breathing room. Fixed width is REQUIRED — without it,
            # Rich's Table.grid auto-sizes the column based on the
            # widest currently-visible row, so a "broken" row scrolling
            # off-screen made other rows' message column SHIFT LEFTWARD
            # by ~3 cells. Pin the kind column → message column always
            # starts at the same screen column. Visual consistency wins.
            tbl.add_column(no_wrap=True, width=11)
            # Message column wraps instead of ellipsising. The user
            # asked for log lines to always be visible — truncating with
            # an ellipsis was hiding important file paths and error
            # text. Trade-off: rows with long messages will be taller,
            # so the panel shows fewer rows when error messages are
            # verbose. That's a fair trade.
            tbl.add_column(overflow="fold", no_wrap=False)            # message
            for ts, kind, msg in s.activity:
                kind_style = {
                    "imported":   self.theme.success,
                    "duplicate":  self.theme.duplicate,
                    "broken":     self.theme.error,
                    "warning":    self.theme.warning,
                    "info":       self.theme.label,
                }.get(kind, self.theme.text)
                ic = kind_icons.get(kind, "")
                kind_label = (f"{ic} {kind}" if ic else kind)
                tbl.add_row(
                    time.strftime("%I:%M:%S %p", time.localtime(ts)).lstrip("0").lower(),
                    Text(kind_label, style=kind_style),
                    Text(msg, style=self.theme.text),
                )
            body = tbl

        return Panel(
            body,
            title="[" + self.theme.border_title + "]Activity[/]",
            border_style=self.theme.border,
            padding=(0, 1),
        )


# =============================================================================
# PLAIN FALLBACK
# =============================================================================

class PlainImportUI:
    """
    Drop-in replacement for LiveImportUI when rich isn't available, or
    when the user explicitly wants plain output (cron jobs, CI, etc.).

    Prints a one-line summary every N file advances. Same public API as
    LiveImportUI so the importer can use either without branching.
    """

    def __init__(self, *, every: int = 25, mode_label: str = "Import", **_ignored):
        self.state = ImportState(mode_label=mode_label)
        self.state.started_at = time.time()
        self._every = every
        self._lock = threading.Lock()

    def __enter__(self):
        print(f"[{self.state.mode_label}] starting...")
        return self

    def __exit__(self, exc_type, exc, tb):
        elapsed = time.time() - self.state.started_at
        print(
            f"[{self.state.mode_label}] done. "
            f"{self.state.files_done} files in {_fmt_duration(elapsed)}. "
            f"imp={self.state.files_imported} "
            f"dup={self.state.files_duplicate} "
            f"bad={self.state.files_broken}"
        )

    def update(self, **kwargs: Any) -> None:
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self.state, k):
                    setattr(self.state, k, v)

    def set_grabbing(self, grabbing: dict[str, Any] | None) -> None:
        with self._lock:
            self.state.grabbing = dict(grabbing or {})

    def set_total(self, total: int) -> None:
        with self._lock:
            self.state.files_total = total

    def set_unit(self, unit: str) -> None:
        with self._lock:
            self.state.unit_label = unit or "files"

    def advance(self, *, imported=False, duplicate=False, broken=False,
                size_bytes=0) -> None:
        with self._lock:
            self.state.files_done += 1
            if imported:  self.state.files_imported += 1
            if duplicate: self.state.files_duplicate += 1
            if broken:    self.state.files_broken += 1
            self.state.bytes_processed += max(0, size_bytes)
            d = self.state.files_done
            if d % self._every == 0 or d == self.state.files_total:
                t = self.state.files_total or d
                pct = (d / t) * 100 if t else 0
                print(
                    f"  [{pct:5.1f}%] {d}/{t}  "
                    f"imp={self.state.files_imported} "
                    f"dup={self.state.files_duplicate} "
                    f"bad={self.state.files_broken}  "
                    f"{os.path.basename(self.state.current_file)[:60]}"
                )

    def log(self, kind: str, message: str) -> None:
        # Print every entry — quiet enough at one line per album.
        print(f"  {kind:>9s}  {message}")


# =============================================================================
# FACTORY
# =============================================================================

def make_ui(
    *,
    theme: str = "cyber",
    mode_label: str = "Import",
    use_rich: bool = True,
    refresh_per_second: int = 20,
    force_terminal: bool = False,
    color_system: str = "auto",
):
    """
    Build an import UI suitable for the current environment.

    - Returns LiveImportUI if rich is installed and use_rich is True.
    - Returns PlainImportUI otherwise.
    """
    if RICH_AVAILABLE and use_rich:
        return LiveImportUI(
            theme=theme,
            mode_label=mode_label,
            refresh_per_second=refresh_per_second,
            force_terminal=force_terminal,
            color_system=color_system,
        )
    return PlainImportUI(mode_label=mode_label)


def list_themes() -> list[str]:
    return sorted(THEMES.keys())
