"""
logo.py
=======

ASCII-art banner rendering for the main menu header.

Two modes:
  1. Use the bundled custom banner (the one shipped with the project) —
     fast, no dep, looks exactly like the original screenshot.
  2. Generate at runtime via pyfiglet, picking from 500+ fonts.

The bundled default is the user's hand-picked "music-organiser / mu"
banner. pyfiglet is a soft dependency: if it's not installed, the font
picker says so and the bundled default is the only option.

Public API:
    render_logo(text="music-organiser", font="default") -> str
    list_fonts() -> list[str]                    # all available figlet fonts
    preview_font(text, font) -> str              # generate one preview
    BUNDLED_BANNERS: dict[str, str]              # name -> raw ASCII

The footer URL line is rendered separately by the caller — keeping it
out of this module so callers can place it wherever they want.
"""

from __future__ import annotations

from typing import Any

try:
    import pyfiglet
    PYFIGLET_AVAILABLE = True
except ImportError:
    pyfiglet = None  # type: ignore
    PYFIGLET_AVAILABLE = False


# =============================================================================
# BUNDLED DEFAULT BANNER
# =============================================================================
#
# This is the hand-picked banner the user supplied. It says
# "music-organiser /mu/" in a custom blocky font. We bundle it inline
# so the menu can render it instantly without invoking pyfiglet on every
# launch (figlet generation is cheap but it's still tens of milliseconds
# for a wide banner, and the menu redraws on every screen entry).
#
# If you want to regenerate from a figlet font instead, set
# config.ui.logo_font to any name from `list_fonts()`.

_BUNDLED_DEFAULT = r"""
                                  ██
 ██████▒                          ██                                                  ░██  ███  ███  ██    ██       ░██
 ███████▒                         ██                           ██                     ██▒  ███  ███  ██    ██       ██▒
 ██   ▒██                                                      ██                    ░██   ███▒▒███  ██    ██      ░██
 ██    ██   ██░████   ░████░    ████      ░████▒     ▓████▒  ███████                 ▓█▒   ███▓▓███  ██    ██      ▓█▒
 ██   ▒██   ███████  ░██████░   ████     ░██████▒   ███████  ███████                 ██    ██▓██▓██  ██    ██      ██
 ███████▒   ███░     ███  ███     ██     ██▒  ▒██  ▓██▒  ▒█    ██                   ▓█▒    ██▒██▒██  ██    ██     ▓█▒
 ██████▒    ██       ██░  ░██     ██     ████████  ██░         ██                   ██     ██░██░██  ██    ██     ██
 ██         ██       ██    ██     ██     ████████  ██          ██                  ▒█▓     ██ ██ ██  ██    ██    ▒█▓
 ██         ██       ██░  ░██     ██     ██        ██░         ██                  ██      ██    ██  ██    ██    ██
 ██         ██       ███  ███     ██     ███░  ▒█  ▓██▒  ░█    ██░                ▒█▓      ██    ██  ██▓  ▓██   ▒█▓
 ██         ██       ░██████░     ██     ░███████   ███████    █████              ██░      ██    ██  ▒██████▒   ██░
 ██         ██        ░████░      ██      ░█████▒    ▓████▒    ░████             ▒██       ██    ██   ▒████▒   ▒██
                                 ▒██                                             ██░                           ██░
                               █████
                               ████░
""".rstrip("\n")


# Keyed registry — "default" maps to the bundled banner. Add more here
# if you ever want to ship extra hand-tuned versions.
BUNDLED_BANNERS: dict[str, str] = {
    "default": _BUNDLED_DEFAULT,
}


# =============================================================================
# RUNTIME GENERATION VIA PYFIGLET
# =============================================================================

# A curated subset of pyfiglet fonts that look good for our use case.
# Used by the font picker as a starter set so the user isn't drowning in
# 500+ choices. `list_fonts()` returns ALL of them; this just biases the
# default list to readable, blocky, banner-style ones.
CURATED_FONTS = [
    "graffiti",           # default — 6-line tag-style, fits at ~100 cols
    "bigmono12",          # wide blocky, similar to the bundled banner
    "ansi_shadow",
    "ansi_regular",
    "ansi_regular_bold",
    "big",
    "bigmono9",
    "block",
    "bloody",
    "doom",
    "epic",
    "isometric1",
    "isometric3",
    "larry3d",
    "ogre",
    "puffy",
    "rectangles",
    "roman",
    "shadow",
    "slant",
    "small",
    "speed",
    "standard",
    "sub-zero",
    "trek",
    "univers",
]


def list_fonts(*, curated_only: bool = True) -> list[str]:
    """
    Return all available logo names. The list always includes 'default'
    (the bundled banner) at the top.

    `curated_only=False` returns the FULL 500+ pyfiglet catalogue.
    """
    fonts: list[str] = list(BUNDLED_BANNERS.keys())
    if not PYFIGLET_AVAILABLE:
        return fonts

    if curated_only:
        # Filter the curated list to only those actually shipping with
        # the installed pyfiglet — different pyfiglet versions ship
        # different subsets.
        available = set(pyfiglet.FigletFont.getFonts())
        fonts += [f for f in CURATED_FONTS if f in available]
    else:
        fonts += sorted(pyfiglet.FigletFont.getFonts())

    return fonts


def preview_font(text: str, font: str, *, canvas_width: int = 240) -> str:
    """
    Render `text` in the named font, returning ASCII. Raises ValueError
    if the font isn't available.

    'default' returns the bundled banner verbatim (ignoring `text`).
    Other names go through pyfiglet with a wide canvas (default 240) so
    long titles render on a single line instead of word-wrapping. Callers
    that want to test wrapping behaviour can pass a narrower canvas_width.
    """
    if font in BUNDLED_BANNERS:
        return BUNDLED_BANNERS[font]
    if not PYFIGLET_AVAILABLE:
        raise ValueError(
            f"font '{font}' requires pyfiglet (not installed). "
            f"Available without pyfiglet: {list(BUNDLED_BANNERS.keys())}"
        )
    try:
        # pyfiglet exposes a Figlet class that takes width; using it
        # avoids the global figlet_format() default of 80.
        fig = pyfiglet.Figlet(font=font, width=canvas_width)
        return fig.renderText(text).rstrip("\n")
    except Exception as e:
        raise ValueError(f"failed to render font '{font}': {e}") from e


def render_logo(
    *,
    text: str = "music-organiser",
    font: str = "default",
    width: int | None = None,
) -> str:
    """
    Top-level: return the logo ASCII for the menu header.

    `width` is the available terminal width. The figlet font is rendered
    on a canvas exactly `width` wide so it never wraps wider than the
    terminal — but if the resulting art is taller than ~25 rows (because
    the title couldn't fit and figlet wrapped it across multiple rows),
    we fall back to a smaller font progressively.
    """
    canvas = width if width is not None else 240

    try:
        art = preview_font(text, font, canvas_width=canvas)
    except ValueError:
        art = BUNDLED_BANNERS["default"]

    # If render is too tall (figlet wrapped because canvas was too narrow
    # for the chosen font), try progressively smaller fonts.
    too_tall = len(art.splitlines()) > 25
    if too_tall and font != "default":
        for fallback in ("big", "standard", "small"):
            try:
                candidate = preview_font(text, fallback, canvas_width=canvas)
                if len(candidate.splitlines()) <= 25:
                    return candidate
            except ValueError:
                continue
        # Last resort: plain text — better than a 60-row banner.
        return f"  {text}"
    return art


def font_works(font: str) -> bool:
    """Quick check: can we render this font without raising?"""
    try:
        preview_font("test", font)
        return True
    except ValueError:
        return False
