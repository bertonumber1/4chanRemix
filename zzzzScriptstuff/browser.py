"""
browser.py
==========

Interactive library browser. A TUI for exploring the database without
writing SQL by hand. Multi-field filter (artist, album, label, genre,
year, codec), live result list, detail popup, export to CSV/JSON.

Keybinds (visible at the bottom of the screen):
    /        focus the search field — typing filters across all visible columns
    a        toggle artist filter prompt
    l        toggle label filter prompt
    A        toggle ALBUM filter prompt
    g        toggle genre filter prompt
    y        toggle year filter prompt
    c        toggle codec filter prompt
    x        clear all filters
    enter    show details popup for the selected row
    e        export current filtered set to CSV
    j / k    move selection down / up
    J / K    page down / page up
    q        back to main menu

Design notes:
  - Built on Rich's Live + Layout. No curses dep. Resize is reactive.
  - All queries go through one SQL builder; safety via parameter binding.
  - We DON'T load the full library into memory — every keystroke that
    changes a filter runs a fresh `SELECT ... WHERE ... LIMIT 500`.
    For a 207k-row library that's the difference between snappy and
    unusable.
"""

from __future__ import annotations

import csv
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class BrowserState:
    """All the filters / cursor / mode that drive the rendering."""
    # Filter values keyed by column name. Empty string = filter off.
    filters: dict[str, str] = field(default_factory=lambda: {
        "artist": "",
        "album": "",
        "label": "",
        "genre": "",
        "year": "",
        "codec": "",
        "free": "",   # free-text search across artist+album+title+label
    })
    # Currently-highlighted result row (0-based index into the loaded rows)
    cursor: int = 0
    # The last loaded rows from the DB. We keep them so cursor navigation
    # doesn't trigger a re-query.
    rows: list[dict[str, Any]] = field(default_factory=list)
    total_matches: int = 0
    page_size: int = 500   # cap per query
    sort_by: str = "artist, album, disc_number, track_number"
    show_detail: bool = False  # is detail popup open?
    # True if the parent process is running in read-only mode (another
    # writer holds the lock). The browser itself only reads, so this is
    # purely informational — we display a banner so the user knows that
    # the file/folder counts they're seeing may shift under them if the
    # writer is mid-import.
    read_only: bool = False


def _build_query(state: BrowserState) -> tuple[str, list[Any]]:
    """Build the SELECT + WHERE for the current filter state. Returns
    (sql, params)."""
    wheres = []
    params: list[Any] = []

    for col in ("artist", "album", "label", "genre", "year", "codec"):
        v = state.filters.get(col, "").strip()
        if v:
            # Year supports exact or range like 1990-1999
            if col == "year" and "-" in v:
                try:
                    lo, hi = v.split("-", 1)
                    wheres.append("substr(year,1,4) BETWEEN ? AND ?")
                    params.extend([lo.strip(), hi.strip()])
                    continue
                except ValueError:
                    pass
            wheres.append(f"{col} LIKE ?")
            params.append(f"%{v}%")

    free = state.filters.get("free", "").strip()
    if free:
        wheres.append(
            "(artist LIKE ? OR album LIKE ? OR title LIKE ? OR label LIKE ?)"
        )
        params.extend([f"%{free}%"] * 4)

    where_clause = ("WHERE " + " AND ".join(wheres)) if wheres else ""
    sql = (
        f"SELECT path, artist, album, title, year, label, genre, codec, "
        f"bitrate, duration_seconds, has_embedded_art, has_folder_art "
        f"FROM files {where_clause} "
        f"ORDER BY {state.sort_by} "
        f"LIMIT {state.page_size}"
    )
    return sql, params


def _count_matches(db, state: BrowserState) -> int:
    sql, params = _build_query(state)
    # Convert the SELECT to a COUNT
    count_sql = sql.split("ORDER BY")[0]
    count_sql = "SELECT COUNT(*) FROM (" + count_sql + ")"
    try:
        return db.conn.execute(count_sql, params).fetchone()[0]
    except Exception:
        return 0


def _refresh(db, state: BrowserState) -> None:
    sql, params = _build_query(state)
    try:
        state.rows = [dict(r) for r in db.conn.execute(sql, params)]
    except Exception as e:
        state.rows = []
    state.total_matches = _count_matches(db, state)
    if state.cursor >= len(state.rows):
        state.cursor = max(0, len(state.rows) - 1)


def _format_duration(seconds: Any) -> str:
    try:
        s = int(round(float(seconds or 0)))
    except (TypeError, ValueError):
        return "?:??"
    if s < 0:
        return "?:??"
    if s >= 3600:
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        return f"{h}:{m:02d}:{sec:02d}"
    m, sec = divmod(s, 60)
    return f"{m}:{sec:02d}"


def _render_table(state: BrowserState) -> Any:
    from rich.table import Table
    from rich.text import Text

    title_parts = [f"results: {len(state.rows):,} shown"]
    if state.total_matches > len(state.rows):
        title_parts.append(f"of {state.total_matches:,} matching")

    t = Table(
        title="  •  ".join(title_parts),
        title_style="bold cyan",
        show_lines=False,
        expand=True,
    )
    t.add_column("#",      style="dim", width=5)
    t.add_column("Artist", style="bold")
    t.add_column("Album",  style="cyan")
    t.add_column("Title")
    t.add_column("Year",   width=6, style="yellow")
    t.add_column("Codec",  width=7, style="green")
    t.add_column("Dur",    width=8, justify="right", style="dim")
    t.add_column("Art",    width=4, justify="center")

    for i, row in enumerate(state.rows):
        is_selected = (i == state.cursor)
        marker = "►" if is_selected else " "

        art_marker = ""
        if row.get("has_embedded_art"):
            art_marker += "E"
        if row.get("has_folder_art"):
            art_marker += "F"

        style = "reverse" if is_selected else None
        t.add_row(
            f"{marker}{i+1:>3}",
            (row.get("artist") or "—")[:22],
            (row.get("album") or "—")[:30],
            (row.get("title") or "—")[:30],
            (row.get("year") or "")[:4],
            (row.get("codec") or "")[:5],
            _format_duration(row.get("duration_seconds")),
            art_marker,
            style=style,
        )
    return t


def _render_filters(state: BrowserState) -> Any:
    from rich.table import Table

    t = Table(show_header=False, show_lines=False, expand=True,
              padding=(0, 1))
    t.add_column("Filter", style="bold cyan", width=10)
    t.add_column("Value", style="white")

    for col_label, col_key in [
        ("artist",  "artist"),
        ("ALBUM",   "album"),
        ("label",   "label"),
        ("genre",   "genre"),
        ("year",    "year"),
        ("codec",   "codec"),
        ("search",  "free"),
    ]:
        v = state.filters.get(col_key, "")
        display = v if v else "—"
        t.add_row(col_label, display)
    return t


def _render_help(read_only: bool = False) -> Any:
    from rich.panel import Panel
    from rich.text import Text
    keys = [
        ("/", "search"), ("a", "artist"), ("A", "Album"),
        ("l", "label"), ("g", "genre"), ("y", "year"), ("c", "codec"),
        ("j/k", "down/up"), ("J/K", "pgdn/pgup"),
        ("enter", "details"), ("e", "export"), ("x", "clear"), ("q", "quit"),
    ]
    txt = Text()
    if read_only:
        txt.append(" READ-ONLY ", style="bold yellow on red")
        txt.append("  another instance is writing  · ",
                    style="bold yellow")
    for i, (k, label) in enumerate(keys):
        txt.append(f" {k}", style="bold yellow")
        txt.append(f"={label}", style="dim")
        if i < len(keys) - 1:
            txt.append("  ")
    return Panel(txt, title="keys", border_style="dim")


def _render_layout(state: BrowserState) -> Any:
    from rich.layout import Layout
    from rich.panel import Panel

    layout = Layout()
    layout.split_column(
        Layout(name="filters", size=10),
        Layout(name="results", ratio=1),
        Layout(name="help", size=4),
    )
    layout["filters"].update(
        Panel(_render_filters(state), title="filters", border_style="cyan")
    )
    layout["results"].update(
        Panel(_render_table(state), title="library", border_style="cyan")
    )
    layout["help"].update(_render_help(read_only=state.read_only))
    return layout


def _show_detail(row: dict[str, Any]) -> None:
    """Print all fields for one row to the screen. The console is
    temporarily released from Live mode by the caller."""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    console.clear()
    t = Table(title=f"Detail — {row.get('artist','?')} / {row.get('title','?')}",
              show_lines=False, expand=True)
    t.add_column("Field", style="bold cyan", width=24)
    t.add_column("Value", style="white")
    for k in sorted(row.keys()):
        v = row[k]
        if v is None or v == "":
            continue
        s = str(v)
        # Pretty-print JSON-looking values
        if s.startswith("{") and s.endswith("}"):
            try:
                parsed = json.loads(s)
                s = json.dumps(parsed, indent=2, ensure_ascii=False)
            except Exception:
                pass
        if len(s) > 1000:
            s = s[:1000] + "..."
        t.add_row(k, s)
    console.print(t)
    console.print()
    console.print("  press enter to return")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass


def _ask_filter_value(filter_name: str, current: str) -> str:
    """Prompt for a new filter value. Empty input clears the filter."""
    print()
    print(f"  filter on {filter_name} (empty to clear)")
    if current:
        print(f"  current: {current}")
    try:
        new = input(f"  {filter_name} > ").strip()
    except (EOFError, KeyboardInterrupt):
        return current
    return new


def _export_csv(state: BrowserState, db) -> None:
    """Re-query (without LIMIT) and dump matching rows to a CSV."""
    from datetime import datetime
    import os

    out_dir = Path.home() / ".local" / "share" / "music-organiser" / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    fn = f"library_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    out_path = out_dir / fn

    sql, params = _build_query(state)
    # Remove the LIMIT to get everything matching
    sql = sql.replace(f"LIMIT {state.page_size}", "")

    try:
        cursor = db.conn.execute(sql, params)
        # Get column names
        col_names = [d[0] for d in cursor.description]
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=col_names)
            writer.writeheader()
            n = 0
            for row in cursor:
                writer.writerow({k: row[k] for k in col_names})
                n += 1
        print()
        print(f"  ✓ exported {n:,} rows to {out_path}")
    except Exception as e:
        print()
        print(f"  ✗ export failed: {e}")
    print("  press enter to continue")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass


def run_browser(db, cfg: dict) -> None:
    """
    Main interactive loop. Uses Rich Live for the redraw, but drops out
    of Live mode whenever it needs to prompt for input (filter values,
    detail popup), because Live and input() don't play well together.
    """
    try:
        from rich.console import Console
        from rich.live import Live
    except ImportError:
        print("  rich not installed — browser unavailable.")
        return

    console = Console()
    state = BrowserState()
    # Mirror the parent process's read-only state into the browser so
    # the help bar can show the banner. This is purely informational —
    # the browser doesn't mutate the DB regardless.
    state.read_only = bool(cfg.get("_runtime", {}).get("read_only", False))
    _refresh(db, state)

    # Manual loop: render with Live for the duration of getch-style key
    # input, then break out to do anything that needs a prompt.
    while True:
        with Live(_render_layout(state), console=console, refresh_per_second=4,
                  screen=True) as live:
            try:
                # We can't get raw keypresses without termios shenanigans
                # in pure-stdlib. So we ask for a one-line command.
                # This means typing 'j' then Enter to scroll down, etc.
                # Not as snappy as vim, but works portably.
                #
                # Render hint: bottom-of-screen status line shown via the
                # _render_help() panel already. Add a prompt line below it.
                live.console.print("\n  key > ", end="")
                live.stop()
                key = input().strip()
            except (EOFError, KeyboardInterrupt):
                key = "q"

        if key == "q":
            return
        elif key == "j":
            if state.cursor < len(state.rows) - 1:
                state.cursor += 1
        elif key == "k":
            if state.cursor > 0:
                state.cursor -= 1
        elif key == "J":
            state.cursor = min(state.cursor + 20, len(state.rows) - 1)
        elif key == "K":
            state.cursor = max(state.cursor - 20, 0)
        elif key == "x":
            for k in state.filters:
                state.filters[k] = ""
            state.cursor = 0
            _refresh(db, state)
        elif key == "/":
            state.filters["free"] = _ask_filter_value("search", state.filters["free"])
            state.cursor = 0
            _refresh(db, state)
        elif key == "a":
            state.filters["artist"] = _ask_filter_value("artist", state.filters["artist"])
            state.cursor = 0
            _refresh(db, state)
        elif key == "A":
            state.filters["album"] = _ask_filter_value("album", state.filters["album"])
            state.cursor = 0
            _refresh(db, state)
        elif key == "l":
            state.filters["label"] = _ask_filter_value("label", state.filters["label"])
            state.cursor = 0
            _refresh(db, state)
        elif key == "g":
            state.filters["genre"] = _ask_filter_value("genre", state.filters["genre"])
            state.cursor = 0
            _refresh(db, state)
        elif key == "y":
            state.filters["year"] = _ask_filter_value("year (e.g. 1992 or 1990-1999)",
                                                       state.filters["year"])
            state.cursor = 0
            _refresh(db, state)
        elif key == "c":
            state.filters["codec"] = _ask_filter_value("codec", state.filters["codec"])
            state.cursor = 0
            _refresh(db, state)
        elif key == "" or key == "enter":
            if state.rows and 0 <= state.cursor < len(state.rows):
                # Re-fetch the FULL row (the SELECT above only grabs visible cols)
                row = state.rows[state.cursor]
                try:
                    full = db.get_by_path(row["path"])
                    if full:
                        row = dict(full)
                except Exception:
                    pass
                _show_detail(row)
        elif key == "e":
            _export_csv(state, db)
        else:
            # Unknown key — just redraw
            pass
