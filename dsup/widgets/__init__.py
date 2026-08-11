"""Fonctions de mise en forme et de rendu partagées par les widgets."""

from __future__ import annotations

from collections.abc import Sequence

from rich.cells import cell_len
from rich.segment import Segment
from rich.style import Style
from textual.strip import Strip

Piece = tuple[str, Style]


def fit(text: str, width: int, *, align: str = "left") -> str:
    """Tronque avec … et complète d'espaces pour occuper exactement `width`."""
    if width <= 0:
        return ""
    if cell_len(text) > width:
        kept = ""
        for char in text:
            if cell_len(kept + char) > width - 1:
                break
            kept += char
        text = kept + "…"
    padding = " " * (width - cell_len(text))
    return text + padding if align == "left" else padding + text


def format_cpu(percent: float | None) -> str:
    return "—" if percent is None else f"{percent:.1f}%"


def format_bytes(value: int | None) -> str:
    if value is None:
        return "—"
    size = float(value)
    unit = "B"
    for unit in ("B", "K", "M", "G", "T"):
        if size < 1024 or unit == "T":
            break
        size /= 1024
    if unit == "B":
        return f"{int(size)}B"
    return f"{size:.1f}{unit}" if size < 10 else f"{round(size)}{unit}"


def format_duration(seconds: float) -> str:
    """Durée compacte façon maquette : 42s, 9m05s, 5h12m, 3d2h, 12d."""
    total = max(0, int(seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days >= 10:
        return f"{days}d"
    if days:
        return f"{days}d{hours}h"
    if hours >= 10:
        return f"{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    if minutes >= 10:
        return f"{minutes}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def format_ago(seconds: float) -> str:
    """« il y a … » pour le panneau détail."""
    total = max(0, int(seconds))
    if total < 60:
        return f"il y a {total} s"
    if total < 3600:
        return f"il y a {total // 60} min"
    if total < 86400:
        return f"il y a {total // 3600} h"
    return f"il y a {total // 86400} j"


def strip_from(
    pieces: Sequence[Piece],
    width: int,
    *,
    pad_style: Style | None = None,
    overlay: Style | None = None,
) -> Strip:
    """Assemble une ligne : styles composés avec `overlay`, complétée à `width`."""
    segments: list[Segment] = []
    for text, style in pieces:
        if not text:
            continue
        segments.append(Segment(text, style + overlay if overlay else style))
    strip = Strip(segments)
    if strip.cell_length > width:
        strip = strip.crop(0, width)
    elif strip.cell_length < width:
        filler = pad_style if pad_style is not None else Style()
        if overlay:
            filler = filler + overlay
        segments = list(strip) + [Segment(" " * (width - strip.cell_length), filler)]
        strip = Strip(segments, width)
    return strip


def bar_pieces(
    left: Sequence[Piece], right: Sequence[Piece], width: int
) -> list[Piece]:
    """Contenu d'une barre : gauche, espace élastique, droite."""
    left_len = sum(cell_len(text) for text, _ in left)
    right_len = sum(cell_len(text) for text, _ in right)
    gap = max(1, width - left_len - right_len)
    return [*left, (" " * gap, Style()), *right]
