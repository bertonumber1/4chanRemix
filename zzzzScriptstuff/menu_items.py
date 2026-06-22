"""
menu_items.py
=============

Single source of truth for every main-menu option. Each entry has:

  - key:         what the user types (e.g. '1', 'b', '?', 'e')
  - icon:        icons.icon() name for the leading glyph
  - label:       short main label (e.g. 'Import')
  - short_hint:  one-liner shown to the RIGHT of the label (truncated
                 if needed to fit terminal width)
  - layman:      one-sentence beginner explanation
  - technical:   nerd-level detail (multiple lines OK)
  - aliases:     extra strings the dispatch accepts
  - kind:        'core' / 'audit' / 'tool' / 'config'
                 — used for grouping in the guide

The main menu uses key/icon/label/short_hint. The guide command uses
layman + technical to print a help screen. The dispatch in organiser.py
uses key + aliases to resolve user input.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MenuItem:
    key: str
    icon: str
    label: str
    short_hint: str
    layman: str
    technical: str
    aliases: set[str] = field(default_factory=set)
    kind: str = "core"


# Ordered list — the main menu renders them in this order.
MENU_ITEMS: list[MenuItem] = [
    MenuItem(
        key="1", icon="download", label="Import",
        short_hint="bring NEW music from your sources into the library",
        layman=(
            "Looks through the folders you told music-organiser to watch "
            "(your USB drives, downloads folder, etc.), reads the tags on "
            "each audio file, and copies or moves the files into one tidy "
            "library tree organised by Artist / Album / Track."
        ),
        technical=(
            "Walks every path in [paths] sources, calls mutagen.File on each "
            "audio file, extracts ~128 columns of metadata (Picard-aligned), "
            "deduplicates by SHA-1 of head+tail+size (fast hash), routes each "
            "file through decide_album_type() to detect solo/mix/dj_set, builds "
            "a destination path via build_destination_path(), and copies or "
            "moves the file. Companion .cue/.log/.nfo files travel with their "
            "audio. Rip-software detection runs during the metadata extract "
            "pass and populates ripper_software/version/confidence columns. "
            "Resumable via checkpoint.py — interrupting and restarting picks "
            "up where it left off."
        ),
        aliases={"imp", "in", "ingest", "add"},
        kind="core",
    ),
    MenuItem(
        key="2", icon="folder_open", label="Organise",
        short_hint="re-sort EXISTING files into clean folders",
        layman=(
            "Take files already in your library and move them into the "
            "right folders based on their tags. Useful after fetching new "
            "metadata — old folders get fixed up."
        ),
        technical=(
            "For every DB row with status='indexed' or 'imported', recomputes "
            "the destination path from current tag values, and moves the file "
            "in place (or copies if mode=copy). Detects compilations via "
            "classify_album() — folders with mixed artists end up in "
            "Various Artists/. Skips files where the path is already correct."
        ),
        aliases={"organize", "reorg", "reorganise", "sort", "move", "restructure"},
        kind="core",
    ),
    MenuItem(
        key="f", icon="config", label="Fix filenames",
        short_hint="rename to ✰ [NN - Artist - Title - Album - Year - CODEC] Ripped By NAME.ext",
        layman=(
            "Rename files in place to a consistent pretty format. Pick a "
            "scope (broken folder only / all files / only ones that don't "
            "match yet) and a strategy (refetch tags first, or use whatever "
            "the database already has). Saves your Soulseek username so "
            "every file ends with 'Ripped By you' for clean attribution."
        ),
        technical=(
            "Uses build_pretty_filename() in organiser_core.py to compute "
            "'✰ [NN - Artist - Title - (freeform) - Album - Year - CODEC] "
            "Ripped By NAME.ext' per row. Full Unicode in filenames (Japanese, "
            "Korean, Cyrillic, accented Latin all preserved); only the 9 "
            "Windows-illegal characters are replaced. Three scopes: "
            "status='broken' only, all DB rows, or only rows where current "
            "basename != target basename. Two strategies: (A) calls "
            "cmd_fetch_metadata first then renames, (B) uses DB tags as-is "
            "and skips rows lacking artist+title. Preserves original_path "
            "and original_filename in the DB via COALESCE set-once. Filesystem "
            "rename via Path.rename; conflict resolution suffixes ' (2)', ' (3)' "
            "etc. Soulseek username persisted to [organise] soulseek_username. "
            "Dry-run preview of first 8 renames before committing."
        ),
        aliases={"rename", "rename-files", "fix-names", "filenames",
                 "fix-filenames", "tidy"},
        kind="tool",
    ),
    MenuItem(
        key="x", icon="warning", label="Fix broken",
        short_hint="rescue pipeline for the Broken folder (refetch, re-eval, reorganise, rename)",
        layman=(
            "One menu choice runs the whole rescue flow for files stuck "
            "in your Broken folder: refetch their tags from providers, "
            "re-evaluate which ones are actually still broken, move "
            "rescued files out to their proper destinations, and "
            "optionally rename them to the pretty format. Each phase "
            "can be skipped individually."
        ),
        technical=(
            "Four-phase pipeline scoped to status='broken' rows OR files "
            "physically under {destination_root}/{broken_folder}/. "
            "Phase 1 chains into cmd_fetch_metadata (uses the existing "
            "fetch checkpoint). Phase 2 calls audit.mark_broken_metadata "
            "bidirectionally — un-breaks rows with all required tags, "
            "re-breaks rows that lost them. Phase 3 calls "
            "organise_in_place which reads each row's lossless flag to "
            "route lossy→Shit Quality and lossless→High Quality. Phase 4 "
            "delegates to cmd_fix_filenames (user picks scope/strategy). "
            "Each phase asks for confirmation; defaults are Yes/Yes/Yes/No."
        ),
        aliases={"fix-broken", "rescue", "unbreak", "broken"},
        kind="tool",
    ),
    MenuItem(
        key="3", icon="database", label="Rebuild database",
        short_hint="re-scan the library to refresh the index",
        layman=(
            "Walk through everything in your library folder right now and "
            "update the database with what's actually on disk. Use this if "
            "you've changed files outside music-organiser."
        ),
        technical=(
            "Calls indexer.index_tree(destination_root) which re-walks the "
            "filesystem, extract_metadata() for each audio file, upsert_file() "
            "for each row. Doesn't delete missing rows by default — use the "
            "Verify pass for stale-row cleanup. Honours [paths] skip_hidden "
            "and follow_symlinks."
        ),
        aliases={"reindex", "re-index", "idx", "index", "rebuild-db",
                  "refresh", "rescan"},
        kind="core",
    ),
    MenuItem(
        key="4", icon="warning", label="Check database (audits)",
        short_hint="find missing tags, dud entries, broken metadata",
        layman=(
            "Runs 13 different checks for problems: missing album art, "
            "missing release year, bogus artist names like '<Unknown>', "
            "files with the same content but different paths, and more. "
            "You can export the lists to CSV."
        ),
        technical=(
            "audit.py module — runs 13 SELECT queries against the DB looking "
            "for: missing embedded art, missing folder art, missing year, "
            "missing genre, missing title, missing album, missing artist, "
            "duplicate content_hash, suspect transcodes, invalid path chars, "
            "stale paths (file no longer exists), broken tags, and files "
            "imported but never organised. Each audit's results can be "
            "exported as CSV/JSON/TXT."
        ),
        aliases={"audit", "checks", "verify-db", "lint"},
        kind="audit",
    ),
    MenuItem(
        key="5", icon="lossy", label="Verify rip authenticity",
        short_hint="spectral check + optional Vamp confirm for fake-FLAC",
        layman=(
            "Looks at the audio inside each FLAC file to see if it's "
            "actually lossless or just an MP3 hiding in a FLAC wrapper. "
            "First pass is quick (FFT-based); if it finds anything, you "
            "can opt to run the slow but accurate Vamp neural-net confirm."
        ),
        technical=(
            "TWO STAGES (combined into one menu option since v0.20):\n"
            "  Stage 1 (FAST, every file): fake_flac.analyse() — opens the "
            "FLAC, computes the FFT of one 4-second window, looks for the "
            "spectral cutoff above ~16kHz that betrays a lossy origin. "
            "Sub-second per file. Marks transcode_suspected=1 on hits.\n"
            "  Stage 2 (SLOW, opt-in, suspects only): rip_audio.run_on_suspects() "
            "shells out to sonic-annotator + vamp-lossy-encoding-detector "
            "plugin (Chris Cannam, QMUL, ICASSP 2017 CNN). ~3 sec/file quick "
            "mode, ~80 sec/file full mode. 98% accurate on first-exposure rips. "
            "Verdicts written to transcode_notes as 'vamp:lossy:0.99'."
        ),
        aliases={"verify", "fake-flac", "fakeflac", "vamp", "rip-audit",
                  "spectral", "rip-check"},
        kind="audit",
    ),
    MenuItem(
        key="6", icon="wrench", label="Fix tags with OneTagger",
        short_hint="launch OneTagger on folders needing work",
        layman=(
            "Opens OneTagger (a separate tool) and points it at the parts of "
            "your library that need attention. OneTagger is great at finding "
            "the right artist/title for badly-named files."
        ),
        technical=(
            "Launches OneTagger via integrations/onetagger.py. Pipes it a list "
            "of folders flagged as having broken or missing core tags. OneTagger "
            "handles the actual MusicBrainz/Beatport/Beatsource resolution and "
            "writes back to files using its own Vorbis/ID3 logic."
        ),
        aliases={"onetagger", "ot", "fix-tags", "tagger"},
        kind="tool",
    ),
    MenuItem(
        key="7", icon="world", label="Fetch metadata online",
        short_hint="fill missing tags from MusicBrainz, Deezer, iTunes...",
        layman=(
            "Reaches out to free music databases to fill in missing info: "
            "year, label, catalog number, MusicBrainz ID, cover art, BPM, "
            "and dozens more. Default is FULL archival mode — get every "
            "field MusicBrainz offers."
        ),
        technical=(
            "metadata_lookup.fill_missing_metadata() with 4-mode preset menu: "
            "QUICK (5 tags) / THOROUGH (12) / FULL (~70 Picard-aligned) / "
            "CUSTOM (pick any DB column). Providers: MusicBrainz, Deezer, "
            "iTunes, Bandcamp; rate-limited per provider; merged via "
            "best_text_value() heuristic. FULL mode uses MB deep_harvest "
            "(inc=labels+recordings+release-groups+artist-credits+isrcs+url-rels"
            "+aliases+annotation+tags+genres+media) — adds ~1.05s/album. "
            "Writes back to file via tag_writer.py (gated by EAC-log presence). "
            "Generates album.nfo per folder. Adds one-line provenance to "
            "the file's COMMENT tag."
        ),
        aliases={"fetch", "metadata", "tags", "mb", "musicbrainz", "lookup"},
        kind="core",
    ),
    MenuItem(
        key="8", icon="table", label="SQL query",
        short_hint="run a SELECT against the DB",
        layman=(
            "If you know SQL, type a query and see the results. Useful for "
            "questions like 'how many FLACs over 1GB do I have?'."
        ),
        technical=(
            "Read-only sqlite3 cursor; SELECT-only enforcement. Prints rows "
            "as a Rich Table with auto-sized columns; offers CSV export."
        ),
        aliases={"sql", "query", "select"},
        kind="tool",
    ),
    MenuItem(
        key="9", icon="trash", label="Compact database",
        short_hint="VACUUM + ANALYZE — reclaim disk + refresh planner stats",
        layman=(
            "Tidy up the database file: makes it smaller on disk and helps "
            "queries run faster. Like defragging, for SQLite."
        ),
        technical=(
            "Runs VACUUM (rewrites the db file, reclaiming free pages) and "
            "ANALYZE (updates sqlite_stat tables so the query planner picks "
            "better indexes). For a 900MB DB this is a ~30sec operation."
        ),
        aliases={"vacuum", "analyze", "analyse", "compact", "optimize", "defrag"},
        kind="tool",
    ),
    MenuItem(
        key="0", icon="config", label="Show config",
        short_hint="print every config setting + its source path",
        layman=(
            "Print the contents of your config.toml so you can see what's "
            "configured. Useful for sanity checks."
        ),
        technical=(
            "Pretty-prints cfg dict, redacts known secret fields, shows "
            "the resolved config-file path so you know which TOML is active."
        ),
        aliases={"config", "show", "settings", "cfg"},
        kind="config",
    ),
    MenuItem(
        key="e", icon="play", label="Do EVERYTHING for me",
        short_hint="Rebuild → Fetch → Organise pipeline (auto or manual)",
        layman=(
            "Runs three steps in a row — rebuild the database, fetch missing "
            "info from MusicBrainz, then organise files into clean folders. "
            "Auto mode uses your saved defaults; manual mode asks before "
            "each step."
        ),
        technical=(
            "cmd_do_everything dispatches Rebuild → Fetch → Organise. "
            "AUTOMATIC mode reads every choice from [last_used] sections of "
            "config.toml; never prompts. MANUAL mode prompts 'do this step?' "
            "for each, and inside each step uses last-used values as the "
            "default but lets you tweak. Errors in one step don't abort the "
            "next — the loop offers to continue."
        ),
        aliases={"do-everything", "do", "all", "pipeline", "everything", "auto"},
        kind="core",
    ),
    MenuItem(
        key="b", icon="search", label="Browse library",
        short_hint="filter/search TUI with detail popup + CSV export",
        layman=(
            "Interactive search through your library. Filter by artist, "
            "album, label, genre, year, or codec. Pick a file to see its "
            "full metadata, or export the current filter to CSV."
        ),
        technical=(
            "browser.py — Rich Live UI, line-input keys (j/k for nav, "
            "/ for free-text search, a/A/l/g/y/c for per-field filters, "
            "enter for detail popup with all 128 columns, e for CSV export). "
            "Year supports ranges (1990-1999). Every keystroke runs a fresh "
            "WHERE+LIMIT 500 query — works snappily even on a 207k-row DB."
        ),
        aliases={"library", "search", "find", "explorer", "browser"},
        kind="tool",
    ),
    MenuItem(
        key="Q", icon="trash", label="Quarantine confirmed lossy",
        short_hint="move vamp-confirmed fake-FLACs out of the library",
        layman=(
            "If the Verify pass confirmed any files are actually MP3s "
            "in a FLAC wrapper, this moves them to a Quarantine folder "
            "so they don't pollute your lossless library. Doesn't delete "
            "them — you decide what to do."
        ),
        technical=(
            "SELECT WHERE transcode_suspected=1 AND transcode_notes LIKE 'vamp:lossy:%' "
            "AND status != 'quarantined'. Each match is moved (atomic rename if "
            "same fs, else copy+delete) into [paths] quarantine_folder. DB "
            "row updated to status='quarantined' + new path. NOT a re-encoder: "
            "re-encoding a fake-FLAC to MP3 won't recover the original; the "
            "lossy information is already gone. Best we can offer is moving "
            "aside for review."
        ),
        aliases={"quarantine", "isolate", "fake-flac-move", "trash-fake"},
        kind="tool",
    ),
    MenuItem(
        key="", icon="warning", label="Transcode confirmed-lossy (advanced)",
        short_hint="opt-in re-encode of fake-FLACs to MP3/Opus — read warnings",
        layman=(
            "Advanced: re-encode confirmed fake-FLACs to a smaller lossy "
            "format like MP3 V0 or Opus 192. Type 'transcode' to invoke. "
            "Has loud warnings because re-encoding lossy-to-lossy is "
            "second-generation quality loss — only use if you'd rather "
            "save disk space than keep the fake-FLAC fiction."
        ),
        technical=(
            "cmd_transcode_suspects: SELECT transcode_suspected=1 AND "
            "(vamp:lossy or confidence >= 0.9). Shells out to ffmpeg per "
            "file with one of: libmp3lame -q:a 0, libmp3lame -b:a 320k, "
            "libopus -b:a 192k, libopus -b:a 128k. Defaults: DRY RUN, "
            "keep_originals=True. Updates path/codec/status='transcoded' "
            "in DB. Renames original to *.flac.bak so you can verify the "
            "output. Not in the main menu — type 'transcode' / 'reencode' "
            "/ 'shrink' to invoke."
        ),
        aliases={"transcode", "reencode", "re-encode", "shrink",
                  "transcode-suspects"},
        kind="tool",
    ),
    MenuItem(
        key="?", icon="info", label="Guide / aliases",
        short_hint="what each option does, layman + technical",
        layman=(
            "Shows this help screen. Every menu item can be typed as its "
            "number, its full name, or a unique prefix (so 'imp' picks 'Import')."
        ),
        technical=(
            "Renders MENU_ITEMS into a layered help screen. Aliases resolve "
            "via the same dispatch table the main menu uses."
        ),
        aliases={"help", "h", "wat", "what", "guide"},
        kind="config",
    ),
    MenuItem(
        key="s", icon="package", label="Re-run first-time setup",
        short_hint="reset sources/destination/preferences",
        layman=(
            "Walks you through the first-run wizard again. Use this if "
            "you've moved your music to a new drive or want to start fresh."
        ),
        technical=(
            "config.run_first_time_setup() — detects OS, asks for confirmation, "
            "rebuilds [paths] sources/destination_root/database, writes config.toml. "
            "Existing [last_used] sections are preserved."
        ),
        aliases={"setup", "first-time", "wizard", "init", "reconfigure"},
        kind="config",
    ),
    MenuItem(
        key="t", icon="config", label="Theme",
        short_hint="change UI colours",
        layman="Pick a different colour theme for the UI.",
        technical="41 themes in ui.THEMES; saved to [ui] theme.",
        aliases={"theme", "colour", "color", "skin"},
        kind="config",
    ),
    MenuItem(
        key="l", icon="config", label="Logo font",
        short_hint="change the figlet font used for the title banner",
        layman="Pick a different ASCII-art font for the music-organiser banner at the top.",
        technical="Curated list of figlet fonts; full 571-font list via 'full' typed at prompt.",
        aliases={"logo", "font", "banner", "figlet"},
        kind="config",
    ),
    MenuItem(
        key="c", icon="config", label="Colour test",
        short_hint="diagnose terminal colour support",
        layman="Shows test patterns so you can see if colours work right in your terminal.",
        technical="Prints $TERM, $COLORTERM, $NO_COLOR, then every theme's palette.",
        aliases={"colour-test", "color-test", "diag", "diagnose"},
        kind="config",
    ),
    MenuItem(
        key="p", icon="play", label="Performance / Speed tier",
        short_hint="Spaceballs LUDICROUS speed config",
        layman=(
            "Switch between safe (slow) and risky (fast) database settings. "
            "Useful when importing huge libraries."
        ),
        technical=(
            "speed.py — toggles SQLite PRAGMA synchronous and journal_mode "
            "across 5 tiers (LIGHT_SPEED → RIDICULOUS_SPEED → "
            "LUDICROUS_SPEED → PLAID). Higher tiers risk DB corruption "
            "on power loss but make bulk inserts 10-50x faster."
        ),
        aliases={"speed", "perf", "performance", "ludicrous", "tier"},
        kind="config",
    ),
    MenuItem(
        key="d", icon="info", label="Debug log",
        short_hint="tail the debug log",
        layman="Show recent debug messages — useful when something went wrong.",
        technical="Tails ~/.cache/music-organiser/debug.log. Cleared on next run.",
        aliases={"debug", "log", "tail"},
        kind="config",
    ),
    MenuItem(
        key="q", icon="cross", label="Quit",
        short_hint="exit music-organiser",
        layman="Exit.",
        technical="SystemExit(0). DB closed cleanly, lockfile removed.",
        aliases={"quit", "exit", "bye", "x"},
        kind="config",
    ),
]


# NOTE: lowercase 'q' is Quit; uppercase 'Q' (or alias 'quarantine'/'isolate')
# is Quarantine. The dispatch is case-sensitive on first character only —
# the rest of an entry like 'QUARANTINE' or 'quarantine' resolves to the
# alias set.


# =============================================================================
# MARQUEE: cycle each menu row's hint between layman / technical
# =============================================================================
#
# Why this exists: the user wants the menu hint area to *teach*. A single
# right-aligned hint can hold ~50 chars on most terminals — not enough
# for both a beginner-friendly sentence AND the technical detail. So we
# cycle through them: layman for ~5s, then chunked technical, then loop.
#
# Each row's clock is staggered by row_index * 0.4s so they don't all
# rotate at once. The page "breathes" left-to-right.

import textwrap as _textwrap
import time as _time


def _marquee_chunks(text: str, width: int) -> list[str]:
    """Word-wrap text into width-fitting chunks."""
    if width <= 10:
        width = 10
    return _textwrap.wrap(text, width=width, break_long_words=False) or [text]


def render_marquee(
    item: MenuItem,
    *,
    row_index: int,
    width: int,
    t: float | None = None,
    layman_duration: float = 5.0,
    chunk_duration: float = 4.0,
    stagger: float = 0.4,
) -> tuple[str, str]:
    """
    For the given row at time t (or now), return (visible_text, mode).
    mode is one of {'layman', 'technical', 'static'} — used by the caller
    to pick a style. 'static' is returned when the layman alone fits and
    there's no technical detail worth cycling through.
    """
    if t is None:
        t = _time.time()
    layman = item.layman.strip()
    technical = item.technical.strip()

    # If layman fits and technical is empty or trivially short, just show layman.
    if len(layman) <= width and not technical:
        return (layman, "static")

    # Truncate layman if needed
    if len(layman) <= width:
        layman_chunk = layman
    else:
        layman_chunk = layman[: width - 1].rstrip() + "…"

    tech_chunks = _marquee_chunks(technical, width) if technical else []
    n_tech = len(tech_chunks)
    if n_tech == 0:
        return (layman_chunk, "static")

    # Total cycle: layman + each tech chunk
    total = layman_duration + n_tech * chunk_duration
    row_t = (t + row_index * stagger) % total

    if row_t < layman_duration:
        return (layman_chunk, "layman")
    row_t -= layman_duration
    chunk_index = int(row_t // chunk_duration)
    if chunk_index >= n_tech:
        chunk_index = n_tech - 1
    return (tech_chunks[chunk_index], "technical")
