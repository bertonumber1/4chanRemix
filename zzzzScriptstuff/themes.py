"""
themes.py
=========

File-based theme system, layered ON TOP of the built-in Theme registry
in ui.py. This module:

  1. Provides gradient interpolation — given 2+ colour stops and a
     step count, produces a smooth multi-stop fade.
  2. Loads themes from TOML files in ~/.config/music-organiser/themes/.
  3. Dumps the built-in themes to that folder so users can edit them.
  4. Returns a Theme instance for any registered name, preferring
     external file over built-in when both exist.

A theme TOML file looks like:

    name = "rainbowdash"

    # Plain colour fields (rich style strings — named, 256, or hex)
    [colours]
    border       = "bright_cyan"
    border_title = "bold magenta"
    accent       = "#ff206e"
    dim          = "grey50"
    text         = "white"
    label        = "cyan"
    success      = "bright_green"
    warning      = "yellow"
    error        = "red"
    duplicate    = "magenta"
    background   = ""              # empty = terminal default

    # Gradient: either give a literal list, OR give stops + steps
    [gradient]
    # Mode 1: literal list (what built-in themes use today)
    colours = ["#ff0000", "#ff8000", "#ffff00", "#00ff00",
                "#0080ff", "#8000ff"]
    # Mode 2: smooth interpolation between stops
    # mode = "smooth"
    # stops = ["#fcba03", "#03fcf0"]
    # steps = 12

    # Optional: per-element symbol overrides (experimental)
    [symbols]
    # Replace the figlet bar separator etc. Keys map to known UI roles.
    # Leave keys absent to use defaults. EXPERIMENTAL — only a few
    # roles are wired into the renderer today (see THEME_SYMBOL_KEYS).
    # border_horizontal = "═"
    # border_corner_tl  = "╔"
    # bullet            = "•"

    # Optional: decoration art (Mane Six, etc.)
    [decoration]
    # Name of a pony art file (under .config/music-organiser/themes/art/)
    # to show in the menu corner when this theme is active.
    # pony = "rainbowdash"
    # position = "top-right"    # or 'top-left', 'bottom-right', ...

Gradient interpolation:
  - "smooth" mode takes N stops and produces `steps` interpolated values
    walking linearly through them (in RGB space). steps=2 -> just the
    endpoints. steps=20 -> 20 colours sweeping the gradient.
  - "discrete" mode just uses the `colours` list as-is.

The dump command writes every built-in theme to disk so users can edit
them. If a file with the same name exists in the user's themes folder,
it overrides the built-in (use the menu's "native vs external" toggle
to switch).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


logger = logging.getLogger("music-organiser")


# Symbol keys recognized by the theme system. Themes can override these
# via [symbols] in their TOML file. Most are currently informational —
# the UI renderer only honours a few at this time. EXPERIMENTAL.
THEME_SYMBOL_KEYS = {
    "border_horizontal": "═",     # the wide horizontal line
    "border_corner_tl":  "╔",
    "border_corner_tr":  "╗",
    "border_corner_bl":  "╚",
    "border_corner_br":  "╝",
    "border_vertical":   "║",
    "bullet":            "•",
    "separator":         "·",
    "checkmark":         "✓",
    "cross":             "✗",
    "ellipsis":          "…",
}


def themes_dir() -> Path:
    """User's themes folder. Created on demand."""
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    p = Path(xdg) / "music-organiser" / "themes"
    p.mkdir(parents=True, exist_ok=True)
    return p


def art_dir() -> Path:
    """Pony/decoration art folder."""
    p = themes_dir() / "art"
    p.mkdir(parents=True, exist_ok=True)
    return p


# =============================================================================
# GRADIENT INTERPOLATION
# =============================================================================

def _hex_to_rgb(hex_or_name: str) -> tuple[int, int, int] | None:
    """Parse a Rich style colour into RGB. Returns None if it's a named
    colour we don't recognise — those stay as-is and bypass interpolation."""
    s = (hex_or_name or "").strip().lower()
    if s.startswith("#") and len(s) in (4, 7):
        try:
            if len(s) == 4:
                # #rgb -> #rrggbb
                r = int(s[1] * 2, 16)
                g = int(s[2] * 2, 16)
                b = int(s[3] * 2, 16)
            else:
                r = int(s[1:3], 16)
                g = int(s[3:5], 16)
                b = int(s[5:7], 16)
            return (r, g, b)
        except ValueError:
            return None
    # Named colours we recognise — could expand, but rely on Rich for the rest
    NAMED = {
        "black": (0, 0, 0),
        "white": (255, 255, 255),
        "red":   (205, 49, 49),
        "green": (13, 188, 121),
        "yellow":(229, 229, 16),
        "blue":  (36, 114, 200),
        "magenta":(188, 63, 188),
        "cyan":  (17, 168, 205),
    }
    return NAMED.get(s)


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*[max(0, min(255, int(c))) for c in rgb])


def _lerp_rgb(a: tuple[int, int, int], b: tuple[int, int, int],
              t: float) -> tuple[int, int, int]:
    return (
        a[0] + (b[0] - a[0]) * t,
        a[1] + (b[1] - a[1]) * t,
        a[2] + (b[2] - a[2]) * t,
    )


def interpolate_gradient(stops: list[str], steps: int) -> list[str]:
    """
    Build a smooth gradient with `steps` colours by walking linearly
    through the given stops.

    - 2 stops, steps=2  -> [stop0, stop1]
    - 2 stops, steps=5  -> 5 evenly-spaced colours including both endpoints
    - 3+ stops          -> stops distributed evenly, interpolation between
                           adjacent stops as needed

    Stops that aren't recognisable RGB get echoed back without
    interpolation (so 'bright_cyan' or 'grey50' still work as stops).
    """
    if not stops:
        return []
    if steps <= 0:
        return []
    if steps == 1:
        return [stops[0]]
    if len(stops) == 1:
        return [stops[0]] * steps

    # Parse stops; for stops we can't parse, fall back to discrete-only
    rgb_stops = [_hex_to_rgb(s) for s in stops]
    if any(r is None for r in rgb_stops):
        # Mixed mode — just echo stops repeated/sliced to fit
        # If steps == len(stops) just return them; otherwise repeat or trim.
        if steps == len(stops):
            return list(stops)
        if steps < len(stops):
            return list(stops)[:steps]
        # repeat last to fill
        out = list(stops)
        while len(out) < steps:
            out.append(stops[-1])
        return out

    # All stops are RGB — do the lerp
    out: list[str] = []
    n_segments = len(rgb_stops) - 1
    for i in range(steps):
        # t in [0, 1] over the full sweep
        t = i / (steps - 1)
        # Which segment are we in?
        seg_t = t * n_segments
        seg_idx = min(int(seg_t), n_segments - 1)
        local_t = seg_t - seg_idx
        a = rgb_stops[seg_idx]
        b = rgb_stops[seg_idx + 1]
        rgb = _lerp_rgb(a, b, local_t)
        out.append(_rgb_to_hex(rgb))
    return out


# =============================================================================
# FILE LOADING / DUMPING
# =============================================================================

def _load_toml(path: Path) -> dict:
    """Load a TOML file using tomllib (read-only, built-in 3.11+)."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib   # type: ignore
    with open(path, "rb") as f:
        return tomllib.load(f)


def _dump_toml(path: Path, data: dict) -> None:
    """Write TOML using tomli_w."""
    try:
        import tomli_w
    except ImportError:
        # Fallback: hand-write a minimal TOML if tomli_w is not available
        with open(path, "w", encoding="utf-8") as f:
            _hand_write_toml(f, data)
        return
    with open(path, "wb") as f:
        tomli_w.dump(data, f)


def _hand_write_toml(f, data: dict, prefix: str = "") -> None:
    """Tiny TOML writer — only supports nested tables and lists of strings.
    Used as a fallback when tomli_w isn't installed."""
    # Top-level scalars first
    for k, v in data.items():
        if isinstance(v, (str, int, float, bool)):
            f.write(f'{k} = {_toml_value(v)}\n')
        elif isinstance(v, list):
            f.write(f'{k} = [{", ".join(_toml_value(x) for x in v)}]\n')
    f.write("\n")
    # Then nested tables
    for k, v in data.items():
        if isinstance(v, dict):
            f.write(f"[{prefix + k}]\n")
            for k2, v2 in v.items():
                if isinstance(v2, (str, int, float, bool)):
                    f.write(f'{k2} = {_toml_value(v2)}\n')
                elif isinstance(v2, list):
                    f.write(f'{k2} = [{", ".join(_toml_value(x) for x in v2)}]\n')
            f.write("\n")


def _toml_value(v) -> str:
    if isinstance(v, str):
        return '"' + v.replace('\\', '\\\\').replace('"', '\\"') + '"'
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


@dataclass
class ThemeSpec:
    """File-based theme. Loaded from TOML; resolved to a `ui.Theme` via
    `to_theme()`."""
    name: str
    colours: dict[str, str] = field(default_factory=dict)
    gradient_mode: str = "discrete"     # 'discrete' or 'smooth'
    gradient_colours: list[str] = field(default_factory=list)
    gradient_stops: list[str] = field(default_factory=list)
    gradient_steps: int = 8
    symbols: dict[str, str] = field(default_factory=dict)
    decoration: dict[str, str] = field(default_factory=dict)

    def resolved_gradient(self) -> list[str]:
        if self.gradient_mode == "smooth" and self.gradient_stops:
            return interpolate_gradient(self.gradient_stops, self.gradient_steps)
        return list(self.gradient_colours)

    @classmethod
    def from_toml(cls, path: Path) -> "ThemeSpec":
        data = _load_toml(path)
        name = data.get("name") or path.stem
        spec = cls(name=name)
        spec.colours = dict(data.get("colours") or {})
        grad = data.get("gradient") or {}
        spec.gradient_mode = grad.get("mode", "discrete")
        spec.gradient_colours = list(grad.get("colours") or [])
        spec.gradient_stops = list(grad.get("stops") or [])
        spec.gradient_steps = int(grad.get("steps", 8))
        spec.symbols = dict(data.get("symbols") or {})
        spec.decoration = dict(data.get("decoration") or {})
        return spec

    def to_dict(self) -> dict:
        """Round-trip back to TOML-ready dict."""
        out: dict = {"name": self.name}
        if self.colours:
            out["colours"] = dict(self.colours)
        grad: dict = {}
        if self.gradient_mode == "smooth":
            grad["mode"] = "smooth"
            if self.gradient_stops:
                grad["stops"] = list(self.gradient_stops)
            grad["steps"] = self.gradient_steps
        if self.gradient_colours:
            grad["colours"] = list(self.gradient_colours)
        if grad:
            out["gradient"] = grad
        if self.symbols:
            out["symbols"] = dict(self.symbols)
        if self.decoration:
            out["decoration"] = dict(self.decoration)
        return out

    def to_theme(self):
        """Convert to a ui.Theme instance."""
        from ui import Theme
        c = self.colours
        # Use built-in theme defaults for any missing fields — fall back to
        # 'default' theme's values rather than failing.
        from ui import THEMES
        fallback = THEMES.get("default")
        return Theme(
            name=self.name,
            border=c.get("border")             or (fallback.border if fallback else "cyan"),
            border_title=c.get("border_title") or (fallback.border_title if fallback else "bold cyan"),
            accent=c.get("accent")             or (fallback.accent if fallback else "magenta"),
            dim=c.get("dim")                   or (fallback.dim if fallback else "grey50"),
            text=c.get("text")                 or (fallback.text if fallback else "white"),
            label=c.get("label")               or (fallback.label if fallback else "cyan"),
            success=c.get("success")           or (fallback.success if fallback else "green"),
            warning=c.get("warning")           or (fallback.warning if fallback else "yellow"),
            error=c.get("error")               or (fallback.error if fallback else "red"),
            duplicate=c.get("duplicate")       or (fallback.duplicate if fallback else "magenta"),
            gradient=self.resolved_gradient() or (fallback.gradient if fallback else ["#888888"] * 4),
            background=(c.get("background") or None),
        )


def list_external_themes() -> list[Path]:
    """All .toml files in the user's themes folder."""
    d = themes_dir()
    return sorted(d.glob("*.toml"))


def load_external_theme(name: str) -> ThemeSpec | None:
    """Load one named theme from disk, or None if not found / unparseable."""
    path = themes_dir() / f"{name}.toml"
    if not path.exists():
        return None
    try:
        return ThemeSpec.from_toml(path)
    except Exception as e:
        logger.warning("themes: %s: %s", path.name, e)
        return None


def dump_builtin_themes(*, overwrite: bool = False) -> dict[str, str]:
    """
    Dump every built-in theme to ~/.config/music-organiser/themes/<name>.toml.
    Returns {name: 'written' | 'skipped (exists)' | 'error: ...'}.
    """
    from ui import THEMES
    out: dict[str, str] = {}
    d = themes_dir()
    for name, theme in THEMES.items():
        path = d / f"{name}.toml"
        if path.exists() and not overwrite:
            out[name] = "skipped (exists)"
            continue
        try:
            spec = ThemeSpec(
                name=name,
                colours={
                    "border":       theme.border,
                    "border_title": theme.border_title,
                    "accent":       theme.accent,
                    "dim":          theme.dim,
                    "text":         theme.text,
                    "label":        theme.label,
                    "success":      theme.success,
                    "warning":      theme.warning,
                    "error":        theme.error,
                    "duplicate":    theme.duplicate,
                    "background":   theme.background or "",
                },
                gradient_mode="discrete",
                gradient_colours=list(theme.gradient),
                gradient_steps=len(theme.gradient),
            )
            _dump_toml(path, spec.to_dict())
            out[name] = "written"
        except Exception as e:
            out[name] = f"error: {e}"
    return out


def list_all_themes() -> dict[str, str]:
    """Return {name: 'native' | 'external' | 'native+external'} for every
    theme available."""
    from ui import THEMES
    builtins = set(THEMES.keys())
    externals = {p.stem for p in list_external_themes()}
    all_names = builtins | externals
    out = {}
    for n in sorted(all_names):
        if n in builtins and n in externals:
            out[n] = "native+external"
        elif n in externals:
            out[n] = "external"
        else:
            out[n] = "native"
    return out


def get_theme(name: str, *, prefer: str = "external") -> Any:
    """
    Resolve a theme name to a ui.Theme. `prefer` is 'external' (default
    — user's file wins) or 'native' (built-in wins; useful for diagnosing
    a broken theme file).
    """
    from ui import THEMES, Theme

    if prefer == "external":
        spec = load_external_theme(name)
        if spec is not None:
            try:
                return spec.to_theme()
            except Exception as e:
                logger.warning("themes: external %s load failed: %s — falling back to native", name, e)
    if name in THEMES:
        return THEMES[name]
    # Last resort: 'default'
    if "default" in THEMES:
        return THEMES["default"]
    raise KeyError(f"no theme named {name!r}")


# =============================================================================
# DECORATION RESOLUTION
# =============================================================================
#
# Built-in themes don't carry decoration info in the Theme dataclass —
# adding fields there would force every theme entry to be updated. Instead
# we keep a static map from theme name → pony art. External themes can
# override this via [decoration] in their TOML.

BUILTIN_THEME_DECORATION = {
    "rainbowdash":  {"pony": "rainbow",     "position": "top-right"},
    "twilight":     {"pony": "twilight",    "position": "top-right"},
    "pinkamena":    {"pony": "pinkie",      "position": "top-right"},
    "rarity":       {"pony": "rarity",      "position": "top-right"},
    "applejack":    {"pony": "applejack",   "position": "top-right"},
    "fluttershy":   {"pony": "fluttershy",  "position": "top-right"},
    "nyancat":      {"pony": "nyancat",     "position": "background"},
}


def get_decoration(name: str) -> dict[str, str]:
    """
    Return decoration config for a theme — keys: pony, position, size.

    Resolution order:
      1. External .toml's [decoration] block (user customisation wins)
      2. Built-in BUILTIN_THEME_DECORATION table
      3. Empty dict (theme has no decoration)
    """
    # External first
    spec = load_external_theme(name)
    if spec is not None and spec.decoration:
        return dict(spec.decoration)
    # Built-in
    return dict(BUILTIN_THEME_DECORATION.get(name.lower(), {}))
