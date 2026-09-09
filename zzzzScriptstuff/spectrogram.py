"""
spectrogram.py
==============

Renders a visual spectrogram (time on X, frequency on Y, energy as colour)
for a single audio file, in the spirit of Spek — but as a small pure-Python
renderer instead of vendoring Spek's C++/GTK codebase, so it stays a normal
pip dependency (numpy + soundfile, both already required by fake_flac.py,
plus Pillow which the project already uses for cover art).

Used by the web UI's Fake-FLAC tab: a suspect file gets an FFT cutoff
number from fake_flac.py, and this module lets you actually *look* at it —
a transcoded file shows a hard horizontal ceiling where a real lossless
file shows energy reaching all the way to the top.

Column count is derived from the requested width, not from a fixed hop
size, so render time and memory stay roughly constant regardless of
whether the file is a 3-minute track or a 90-minute mix — we sample
`width` evenly-spaced windows across the whole file rather than an STFT
over every hop.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    np = None  # type: ignore
    NUMPY_AVAILABLE = False

try:
    import soundfile as sf
    SOUNDFILE_AVAILABLE = True
except ImportError:
    sf = None  # type: ignore
    SOUNDFILE_AVAILABLE = False

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    Image = ImageDraw = ImageFont = None  # type: ignore
    PIL_AVAILABLE = False


FFT_SIZE = 2048
NOISE_FLOOR_DB = -80.0
MARGIN_LEFT = 46     # room for frequency axis labels
MARGIN_BOTTOM = 22   # room for time axis labels
MARGIN_RIGHT = 8
MARGIN_TOP = 8

_CACHE_ROOT = Path("~/.cache/music-organiser/spectrograms").expanduser()

# Colour stops for dB -> RGB, black -> blue -> green -> yellow -> white.
# Roughly matches the classic Spek/sox "heat" look without needing matplotlib.
_STOPS_POS = [0.00, 0.20, 0.45, 0.65, 0.85, 1.00]
_STOPS_RGB = [(0, 0, 0), (0, 0, 90), (0, 120, 180),
              (0, 200, 120), (255, 230, 0), (255, 255, 255)]


def dependencies_available() -> bool:
    return NUMPY_AVAILABLE and SOUNDFILE_AVAILABLE and PIL_AVAILABLE


def missing_dependencies() -> list[str]:
    miss = []
    if not NUMPY_AVAILABLE:
        miss.append("numpy")
    if not SOUNDFILE_AVAILABLE:
        miss.append("soundfile")
    if not PIL_AVAILABLE:
        miss.append("Pillow")
    return miss


def _colormap(norm: Any) -> Any:
    """norm: float array in [0, 1], any shape. Returns uint8 array (*shape, 3)."""
    positions = np.array(_STOPS_POS)
    colors = np.array(_STOPS_RGB, dtype=np.float32)
    channels = [np.interp(norm, positions, colors[:, c]) for c in range(3)]
    return np.stack(channels, axis=-1).astype(np.uint8)


def _analyse_columns(path: Path, width_cols: int, fft_size: int) -> tuple[Any, int, float]:
    """
    Sample `width_cols` evenly-spaced FFT windows across the whole file.
    Returns (db_array[freq_bins, width_cols], sample_rate, duration_seconds).
    """
    with sf.SoundFile(str(path)) as f:
        total_frames = len(f)
        sample_rate = f.samplerate
        if total_frames <= 0 or sample_rate <= 0:
            raise ValueError("empty or unreadable audio stream")

        n_cols = max(1, min(width_cols, total_frames))
        hop = max(1, (total_frames - fft_size) // n_cols) if total_frames > fft_size else 1
        window = np.hanning(fft_size)
        n_freq_bins = fft_size // 2 + 1
        columns = np.empty((n_freq_bins, n_cols), dtype=np.float32)

        for col in range(n_cols):
            start = min(col * hop, max(0, total_frames - fft_size))
            f.seek(start)
            data = f.read(frames=fft_size, dtype="float32", always_2d=True)
            mono = data.mean(axis=1) if data.ndim == 2 else data
            if len(mono) < fft_size:
                mono = np.pad(mono, (0, fft_size - len(mono)))
            spectrum = np.fft.rfft(mono * window)
            magnitude = np.maximum(np.abs(spectrum), 1e-12)
            columns[:, col] = 20.0 * np.log10(magnitude)

        duration_seconds = total_frames / sample_rate
        return columns, sample_rate, duration_seconds


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _fmt_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def render_spectrogram(
    path: str | Path,
    *,
    width: int = 1200,
    height: int = 480,
    fft_size: int = FFT_SIZE,
    noise_floor_db: float = NOISE_FLOOR_DB,
):
    """Render a spectrogram PNG (PIL Image) for `path`. Raises on failure —
    callers should catch and surface the error, there's no silent fallback
    image."""
    if not dependencies_available():
        raise RuntimeError(
            f"spectrogram rendering needs: {', '.join(missing_dependencies())}"
        )

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(str(p))

    columns, sample_rate, duration_seconds = _analyse_columns(p, width, fft_size)
    n_freq_bins, n_cols = columns.shape

    peak_db = float(columns.max())
    db = np.clip(columns - peak_db, noise_floor_db, 0.0)
    norm = (db - noise_floor_db) / (-noise_floor_db)  # 0..1, 0=silence 1=peak

    rgb = _colormap(norm)                 # (freq_bins, cols, 3), row 0 = 0 Hz
    rgb = rgb[::-1, :, :]                 # flip so low freq is at the bottom
    plot = Image.fromarray(rgb, mode="RGB").resize((width, height), Image.BILINEAR)

    canvas = Image.new("RGB", (width + MARGIN_LEFT + MARGIN_RIGHT,
                                height + MARGIN_TOP + MARGIN_BOTTOM), (18, 18, 26))
    canvas.paste(plot, (MARGIN_LEFT, MARGIN_TOP))

    draw = ImageDraw.Draw(canvas)
    font = _font(11)
    nyquist = sample_rate / 2.0

    # Frequency gridlines — every ~4 kHz, labelled on the left margin.
    step_hz = 4000
    hz = 0
    while hz <= nyquist:
        frac = hz / nyquist
        y = MARGIN_TOP + int(height * (1.0 - frac))
        draw.line([(MARGIN_LEFT, y), (MARGIN_LEFT + width, y)],
                  fill=(255, 255, 255, 40), width=1)
        label = f"{hz // 1000}k" if hz else "0"
        draw.text((2, max(0, y - 6)), label, fill=(160, 160, 170), font=font)
        hz += step_hz

    # Time ticks along the bottom.
    n_ticks = min(8, max(2, width // 140))
    for i in range(n_ticks + 1):
        frac = i / n_ticks
        x = MARGIN_LEFT + int(width * frac)
        t = duration_seconds * frac
        draw.line([(x, MARGIN_TOP), (x, MARGIN_TOP + height)],
                  fill=(255, 255, 255, 25), width=1)
        draw.text((min(x, MARGIN_LEFT + width - 28), MARGIN_TOP + height + 4),
                  _fmt_time(t), fill=(160, 160, 170), font=font)

    return canvas


def _cache_key(path: Path, width: int, height: int) -> str:
    st = path.stat()
    raw = f"{path.resolve()}|{st.st_mtime_ns}|{st.st_size}|{width}x{height}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def get_or_render(
    path: str | Path,
    *,
    width: int = 1200,
    height: int = 480,
    force: bool = False,
) -> Path:
    """Return a cached PNG path for `path`'s spectrogram, rendering + caching
    it first if needed. Cache key includes mtime+size, so an edited/replaced
    file re-renders automatically."""
    p = Path(path).resolve()
    _CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    cache_path = _CACHE_ROOT / f"{_cache_key(p, width, height)}.png"
    if cache_path.exists() and not force:
        return cache_path
    img = render_spectrogram(p, width=width, height=height)
    tmp = cache_path.with_suffix(".tmp.png")
    img.save(tmp, format="PNG")
    tmp.replace(cache_path)
    return cache_path
