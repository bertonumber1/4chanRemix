"""
decoration.py
=============

Optional decorative ASCII / ANSI art that themes can request. Today
this is the Mane Six pony art (ponysay format) plus a static nyancat
watermark. Themes reference these via [decoration] in their TOML.

The deal:
  - Bundled art lives at ./data/ponies/<name>.pony (Mane Six + nyancat)
  - User can drop their own .pony files at $XDG_CONFIG/music-organiser/themes/art/
  - Anything in the user's art/ dir wins over bundled

Honest scope notes:
  - This is one static frame per pony — no animation.
  - Nyancat IS animated in the original C program. I considered porting
    the animation, but compositing live ANSI art behind our Rich panels
    would require setting bg on every text cell. That's a much bigger
    refactor. So: ONE nyancat frame, watermark style. If you want the
    real animation you can run nyancat standalone in a side terminal.
  - Pony art is ~22 lines tall. Themes can request 'half' size (top
    half only) for tighter layouts.

The ponysay format:
    $$$
    KEY: value
    KEY: value
    ...
    $$$
    <ANSI 256-color art with $\\$ and $balloon\\d+$ placeholders>

We strip the metadata block and balloon placeholders, leaving the
rendered ANSI art ready to print.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Iterable


logger = logging.getLogger("music-organiser")


# Mane Six + a few extras we bundle. Keys are the names themes refer
# to in their [decoration] pony=... field.
KNOWN_PONIES = [
    "rainbow",     # Rainbow Dash (canonical short name in ponysay)
    "twilight",    # Twilight Sparkle
    "pinkie",      # Pinkie Pie
    "rarity",
    "applejack",
    "fluttershy",
    # Add aliases users might naturally type
]
PONY_ALIASES = {
    "rainbowdash": "rainbow",
    "rainbow_dash": "rainbow",
    "rd": "rainbow",
    "twilightsparkle": "twilight",
    "twi": "twilight",
    "pinkiepie": "pinkie",
    "pp": "pinkie",
    "aj": "applejack",
    "shy": "fluttershy",
    "flutters": "fluttershy",
}


def _bundled_dir() -> Path:
    """Where bundled .pony files ship. Sits next to this module."""
    return Path(__file__).resolve().parent / "data" / "ponies"


def _user_art_dir() -> Path | None:
    """User's drop-in folder. Created on demand if themes module is around."""
    try:
        from themes import art_dir
        return art_dir()
    except Exception:
        # themes module missing — no user dir
        xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        p = Path(xdg) / "music-organiser" / "themes" / "art"
        if p.exists():
            return p
        return None


def _resolve_name(name: str) -> str:
    """Map aliases to canonical pony names."""
    n = (name or "").lower().strip()
    return PONY_ALIASES.get(n, n)


def find_pony_file(name: str) -> Path | None:
    """Locate a pony art file by name. User's art/ folder wins, then bundled."""
    canon = _resolve_name(name)
    # Try user dir first
    user = _user_art_dir()
    if user is not None:
        candidate = user / f"{canon}.pony"
        if candidate.exists():
            return candidate
    # Bundled
    bundled = _bundled_dir() / f"{canon}.pony"
    if bundled.exists():
        return bundled
    return None


def list_available_ponies() -> list[str]:
    """All ponies that can be loaded right now (bundled + user art)."""
    names: set[str] = set()
    for p in _bundled_dir().glob("*.pony"):
        names.add(p.stem)
    user = _user_art_dir()
    if user is not None:
        for p in user.glob("*.pony"):
            names.add(p.stem)
    return sorted(names)


def parse_pony(path: Path) -> tuple[dict[str, str], str]:
    """
    Parse a .pony file into (metadata, ansi_art).
    The art has balloon placeholders stripped and leading/trailing blanks
    trimmed. ANSI escape sequences are preserved verbatim.
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    parts = raw.split("$$$")
    # ponysay layout: ['', meta, art, ...] — splits are at '$$$' literals
    if len(parts) < 3:
        return {}, raw
    meta_block = parts[1]
    art = "$$$".join(parts[2:])

    # Metadata: KEY: value, one per line
    meta: dict[str, str] = {}
    for line in meta_block.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()

    # Strip balloon placeholders. Cowsay/ponysay uses these to anchor the
    # speech bubble. We're not showing dialogue, so drop them.
    art = re.sub(r"\$\\\$", " ", art)
    art = re.sub(r"\$balloon\d+\$", "", art)
    art = art.lstrip("\n")

    # Trim leading/trailing blank rows — the balloon area is gone, no
    # need to keep empty space at the top.
    lines = art.split("\n")

    def _is_blank(line: str) -> bool:
        # Strip ANSI escapes before checking
        cleaned = re.sub(r"\x1b\[[\d;]*m", "", line)
        return not cleaned.strip()

    while lines and _is_blank(lines[0]):
        lines.pop(0)
    while lines and _is_blank(lines[-1]):
        lines.pop()
    return meta, "\n".join(lines)


def load_pony(name: str, *, size: str = "full") -> tuple[dict[str, str], list[str]]:
    """
    Load a pony by name. Returns (metadata, art_lines).

    size:
      - 'full' (default) — every line of the art
      - 'half' — top half only (useful for narrow layouts; cuts off legs)
      - 'tiny' — first 5 lines only (just the head/shoulders)
    """
    path = find_pony_file(name)
    if path is None:
        logger.debug("decoration: no pony art file for %r", name)
        return {}, []
    try:
        meta, art = parse_pony(path)
    except Exception as e:
        logger.warning("decoration: failed to parse %s: %s", path.name, e)
        return {}, []
    lines = art.split("\n")
    if size == "half":
        lines = lines[: max(1, len(lines) // 2)]
    elif size == "tiny":
        lines = lines[:5]
    return meta, lines


# =============================================================================
# NYANCAT — static frame watermark
# =============================================================================
#
# The original nyancat (kuroi-usagi/nyancat-master) renders an animated
# pop-tart cat with a rainbow trail by repainting the whole screen ~10
# times/sec via curses-style ANSI cursor positioning. Compositing live
# animation behind our Rich panels would require:
#   1. Setting bg-colour on every text cell (otherwise the cat shows
#      through transparent cells)
#   2. Coordinating two repaint rates without races
#   3. Handling resize for both layers
# That's a multi-day refactor. So instead we ship ONE STATIC frame as a
# watermark — dim, behind text. If you want the real animation, run
# nyancat in another terminal pane.

# Hand-crafted nyancat watermark — pop-tart body + rainbow trail.
# Uses Rich-friendly markup (we render via Text.from_markup downstream
# when this string is fed through ui.py). The art itself is plain ASCII
# so it works without Nerd Fonts.
NYANCAT_WATERMARK = """\
[dim red]~~~~~~~~~~~~~~~~~[/] [yellow on white]┌─[/][black on white]●[/][yellow on white] [/][black on white]●[/][yellow on white]─┐[/]
[dim orange1]~~~~~~~~~~~~~~~~~[/] [yellow on white]│ [/][magenta on white]・[/][yellow on white] ▼ │[/]
[dim yellow]~~~~~~~~~~~~~~~~~[/] [yellow on white]└─[/][magenta on white]w[/][yellow on white]w[/][magenta on white]w[/][yellow on white]┘[/]
[dim green]~~~~~~~~~~~~~~~~~[/]    [white]/ \\[/]   [white]/ \\[/]
[dim blue]~~~~~~~~~~~~~~~~~[/]
[dim purple]~~~~~~~~~~~~~~~~~[/]
"""


def get_nyancat_watermark() -> str:
    """Return the Rich-markup nyancat watermark string. Used by themes
    that set [decoration] pony = 'nyancat' for the corner art slot.
    Static — see module docstring for why."""
    return NYANCAT_WATERMARK


# =============================================================================
# RENDER HELPER
# =============================================================================

def render_pony_panel(name: str, *, size: str = "full",
                       max_width: int | None = None) -> str | None:
    """
    Return the pony art as a plain ANSI string ready to print or wrap
    in a Rich Panel. The art still contains raw ANSI escape codes (256
    colour) which most terminals render correctly.

    Returns None if the named pony can't be found.
    """
    if name == "nyancat":
        return NYANCAT_WATERMARK
    _meta, lines = load_pony(name, size=size)
    if not lines:
        return None
    if max_width is not None:
        # Be conservative: truncate raw lines that exceed width. We don't
        # try to parse the ANSI to keep colours intact — at the edge, the
        # terminal handles the cut.
        lines = [ln if len(ln) <= max_width else ln[:max_width] for ln in lines]
    return "\n".join(lines)
