"""Panneau de logs : tampon circulaire borné, suivi, recherche, wrap, sélection."""

from __future__ import annotations

import re
import threading
from bisect import bisect_left
from collections import deque
from typing import NamedTuple

from rich.style import Style
from textual.strip import Strip
from textual.widget import Widget

from dsup import theme
from dsup.widgets import Piece, strip_from

LEVEL_NORMAL = 0
LEVEL_WARN = 1
LEVEL_ERROR = 2

_TS_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T(\d{2}:\d{2}:\d{2})\S*)\s")
_WARN_RE = re.compile(r"(?i)\bwarn(ing)?\b")
_ERROR_RE = re.compile(r"(?i)\b(error|err|fatal|panic|critical)\b|\s5\d{2}\s")

_LEVEL_STYLES: tuple[Style, ...] = (theme.TEXT, theme.S_ORANGE, theme.S_RED)


class LogLine(NamedTuple):
    """Une ligne décodée : horodatage court, texte, niveau de gravité."""

    ts: str
    text: str
    level: int


def _classify(text: str) -> int:
    if _ERROR_RE.search(text):
        return LEVEL_ERROR
    if _WARN_RE.search(text):
        return LEVEL_WARN
    return LEVEL_NORMAL


class LogBuffer:
    """Tampon circulaire borné, alimenté par morceaux bruts du flux Docker.

    `dropped` compte les lignes évincées depuis le début : la ligne d'indice
    i du tampon porte le numéro absolu `dropped + i`, qui ne bouge pas quand
    le tampon plein fait tourner ses indices.
    """

    def __init__(self, maxlen: int = 2000) -> None:
        self._lines: deque[LogLine] = deque(maxlen=maxlen)
        self._partial = b""
        # feed() tourne dans le thread lecteur : le verrou garantit qu'une
        # fenêtre lue côté interface associe le bon `dropped` à ses lignes.
        self._lock = threading.Lock()
        self.version = 0
        self.dropped = 0

    def feed(self, chunk: bytes) -> None:
        """Découpe le flux en lignes ; l'éventuel reste attend le prochain chunk."""
        data = self._partial + chunk
        parts = data.split(b"\n")
        self._partial = parts.pop()
        fresh: list[LogLine] = []
        for raw in parts:
            line = raw.decode("utf-8", errors="replace").rstrip("\r")
            ts = ""
            match = _TS_PREFIX_RE.match(line)
            if match is not None:
                ts = match.group(2)
                line = line[match.end() :]
            fresh.append(LogLine(ts, line, _classify(line)))
        if not fresh:
            return
        with self._lock:
            before = len(self._lines)
            self._lines.extend(fresh)
            self.dropped += before + len(fresh) - len(self._lines)
            self.version += 1

    def __len__(self) -> int:
        return len(self._lines)

    def snapshot(self) -> tuple[LogLine, ...]:
        with self._lock:
            return tuple(self._lines)

    def window(self) -> tuple[int, tuple[LogLine, ...]]:
        """Numéro absolu de la première ligne, et les lignes elles-mêmes."""
        with self._lock:
            return self.dropped, tuple(self._lines)


class LogView(Widget):
    """Affiche un LogBuffer ; défiler vers le haut coupe le suivi, G/f le reprennent.

    Le mode sélection (`start_selection`) délimite des lignes entières entre
    une ancre et un curseur, repérés par numéro absolu : la sélection reste
    sur le même texte quand de nouvelles lignes évincent les plus anciennes.
    """

    DEFAULT_CSS = "LogView { width: 1fr; height: 1fr; }"

    def __init__(self) -> None:
        super().__init__()
        self._buffer: LogBuffer | None = None
        self.follow = True
        self.wrap = False
        self._offset = 0
        self._query = ""
        self._matches: list[int] = []
        self._match_pos = -1
        self._cache_key: tuple[int, int, bool] | None = None
        self._first = 0
        self._lines: tuple[LogLine, ...] = ()
        self._vrows: list[tuple[int, int, int]] = []
        self._anchor: int | None = None
        self._cursor = 0
        self._follow_before = False

    # ------------------------------------------------------------- API écran

    def attach(self, buffer: LogBuffer) -> None:
        self._buffer = buffer
        self._cache_key = None
        self._first = buffer.dropped
        self._lines = ()
        self._vrows = []
        self.refresh()

    def on_new_lines(self) -> None:
        if self._query:
            self._matches = self._compute_matches()
        self.refresh()

    def scroll_by(self, delta: int) -> None:
        rows = self._rows()
        current = self._current_offset(rows)
        self._offset = max(0, min(current + delta, self._max_offset(rows)))
        if delta < 0:
            self.follow = False
        self.refresh()

    def goto_top(self) -> None:
        self.follow = False
        self._offset = 0
        self.refresh()

    def goto_end(self) -> None:
        self.follow = True
        self.refresh()

    def toggle_follow(self) -> None:
        self.follow = not self.follow
        self.refresh()

    def toggle_wrap(self) -> None:
        self.wrap = not self.wrap
        self.refresh()

    @property
    def match_count(self) -> int:
        return len(self._matches)

    @property
    def match_position(self) -> int:
        return self._match_pos + 1 if self._match_pos >= 0 else 0

    def set_search(self, query: str) -> None:
        """Recherche incrémentale : surligne et saute au premier résultat visible."""
        self._query = query
        self._matches = self._compute_matches()
        self._match_pos = -1
        if query and self._matches:
            rows = self._rows()
            top_offset = self._current_offset(rows)
            top_line = rows[top_offset][0] if top_offset < len(rows) else 0
            self._match_pos = next(
                (i for i, li in enumerate(self._matches) if li >= top_line), 0
            )
            self._center_on(self._matches[self._match_pos])
        self.refresh()

    def next_match(self, direction: int) -> None:
        if not self._matches:
            return
        self._match_pos = (self._match_pos + direction) % len(self._matches)
        self._center_on(self._matches[self._match_pos])
        self.refresh()

    # ------------------------------------------------------------ sélection

    @property
    def selecting(self) -> bool:
        return self._anchor is not None

    @property
    def selection_size(self) -> int:
        bounds = self._selection_bounds()
        return 0 if bounds is None else bounds[1] - bounds[0] + 1

    def start_selection(self) -> bool:
        """Entre en sélection, curseur sur le résultat courant ou la dernière
        ligne visible ; fige l'affichage. False si le tampon est vide."""
        rows = self._rows()
        if not rows:
            return False
        offset = self._current_offset(rows)
        height = max(1, self.size.height)
        top_line = rows[offset][0]
        bottom_line = rows[min(offset + height, len(rows)) - 1][0]
        line = bottom_line
        if self._query and self._match_pos >= 0:
            match_line = self._matches[self._match_pos]
            if top_line <= match_line <= bottom_line:
                line = match_line
        self._follow_before = self.follow
        self.follow = False
        self._offset = offset
        self._anchor = self._cursor = self._first + line
        self.refresh()
        return True

    def move_cursor(self, delta: int) -> None:
        self._rows()
        self._set_cursor(self._cursor + delta)

    def cursor_to_start(self) -> None:
        self._rows()
        self._set_cursor(self._first)

    def cursor_to_end(self) -> None:
        self._rows()
        self._set_cursor(self._first + len(self._lines) - 1)

    def select_all(self) -> None:
        self._rows()
        if self._anchor is None or not self._lines:
            return
        self._anchor = self._first
        self._set_cursor(self._first + len(self._lines) - 1)

    def selected_text(self) -> tuple[str, int]:
        """Lignes sélectionnées, entières et telles qu'affichées (wrap ou non)."""
        self._rows()
        bounds = self._selection_bounds()
        if bounds is None:
            return "", 0
        low, high = bounds
        chosen = self._lines[low : high + 1]
        return "\n".join(_display(line) for line in chosen), len(chosen)

    def end_selection(self) -> None:
        """Quitte la sélection et rend le suivi s'il était actif avant."""
        if self._anchor is None:
            return
        self._anchor = None
        self.follow = self._follow_before
        self.refresh()

    def _set_cursor(self, absolute: int) -> None:
        if self._anchor is None or not self._lines:
            return
        last = self._first + len(self._lines) - 1
        self._cursor = max(self._first, min(absolute, last))
        self._ensure_visible(self._cursor - self._first)
        self.refresh()

    def _selection_bounds(self) -> tuple[int, int] | None:
        """Indices (dans self._lines) de la sélection ; None si vide ou évincée."""
        if self._anchor is None or not self._lines:
            return None
        low = min(self._anchor, self._cursor) - self._first
        high = max(self._anchor, self._cursor) - self._first
        if high < 0:
            return None
        return max(0, low), min(high, len(self._lines) - 1)

    # ---------------------------------------------------------------- rendu

    def render_line(self, y: int) -> Strip:
        width = max(1, self.size.width)
        rows = self._rows()
        index = self._current_offset(rows) + y
        if index >= len(rows):
            return strip_from([("~", theme.S_DIM)], width)
        line_index, start, end = rows[index]
        bounds = self._selection_bounds()
        selected = bounds is not None and bounds[0] <= line_index <= bounds[1]
        is_cursor = selected and self._first + line_index == self._cursor
        return strip_from(
            self._slice_pieces(line_index, start, end, selected, is_cursor),
            width,
            pad_style=theme.REVERSE if selected else None,
        )

    def _current_offset(self, rows: list[tuple[int, int, int]]) -> int:
        max_offset = self._max_offset(rows)
        return max_offset if self.follow else min(self._offset, max_offset)

    def _max_offset(self, rows: list[tuple[int, int, int]]) -> int:
        return max(0, len(rows) - max(1, self.size.height))

    def _rows(self) -> list[tuple[int, int, int]]:
        buffer = self._buffer
        if buffer is None:
            return []
        width = max(1, self.size.width)
        key = (buffer.version, width, self.wrap)
        if key != self._cache_key:
            self._cache_key = key
            first, self._lines = buffer.window()
            evicted = first - self._first
            if evicted > 0 and not self.follow:
                # Le tampon plein a évincé des lignes en tête : sans ce
                # recalage, la vue figée glisserait d'autant vers le bas.
                gone = bisect_left(self._vrows, (evicted, -1, -1))
                self._offset = max(0, self._offset - gone)
            self._first = first
            vrows: list[tuple[int, int, int]] = []
            for i, line in enumerate(self._lines):
                length = len(_display(line))
                if not self.wrap or length <= width:
                    vrows.append((i, 0, max(length, 1)))
                else:
                    start = 0
                    while start < length:
                        vrows.append((i, start, min(start + width, length)))
                        start += width
            self._vrows = vrows
        return self._vrows

    def _slice_pieces(
        self,
        line_index: int,
        start: int,
        end: int,
        selected: bool = False,
        is_cursor: bool = False,
    ) -> list[Piece]:
        line = self._lines[line_index]
        display = _display(line)
        prefix = len(line.ts) + 1 if line.ts else 0
        level_style = _LEVEL_STYLES[line.level]
        highlights = self._match_ranges(display) if self._query else []
        boundaries = {start, end, min(max(prefix, start), end)}
        for a, b in highlights:
            if a < end and b > start:
                boundaries.add(max(a, start))
                boundaries.add(min(b, end))
        cuts = sorted(boundaries)
        pieces: list[Piece] = []
        for a, b in zip(cuts, cuts[1:]):
            if a >= b:
                continue
            style = theme.S_GREY if a < prefix else level_style
            if is_cursor:
                style = style + theme.S_BOLD
            highlighted = any(ha <= a and b <= hb for ha, hb in highlights)
            # Vidéo inversée en « ou exclusif » : un résultat de recherche
            # reste repérable à l'intérieur d'une sélection, elle-même inversée.
            if highlighted != selected:
                style = style + theme.REVERSE
            pieces.append((display[a:b], style))
        return pieces or [("", theme.TEXT)]

    def _match_ranges(self, display: str) -> list[tuple[int, int]]:
        query = self._query.lower()
        if not query:
            return []
        lowered = display.lower()
        ranges: list[tuple[int, int]] = []
        position = 0
        while (found := lowered.find(query, position)) != -1:
            ranges.append((found, found + len(query)))
            position = found + len(query)
        return ranges

    def _compute_matches(self) -> list[int]:
        buffer = self._buffer
        query = self._query.lower()
        if buffer is None or not query:
            return []
        return [
            i
            for i, line in enumerate(buffer.snapshot())
            if query in _display(line).lower()
        ]

    def _center_on(self, line_index: int) -> None:
        rows = self._rows()
        height = max(1, self.size.height)
        row = next((i for i, (li, _, _) in enumerate(rows) if li == line_index), 0)
        self.follow = False
        self._offset = max(0, min(row - height // 2, self._max_offset(rows)))

    def _ensure_visible(self, line_index: int) -> None:
        """Défile au minimum pour que la ligne `line_index` soit à l'écran."""
        rows = self._rows()
        height = max(1, self.size.height)
        first_row = bisect_left(rows, (line_index, -1, -1))
        last_row = bisect_left(rows, (line_index + 1, -1, -1)) - 1
        offset = self._current_offset(rows)
        if first_row < offset:
            offset = first_row
        elif last_row >= offset + height:
            offset = min(first_row, last_row - height + 1)
        self.follow = False
        self._offset = max(0, min(offset, self._max_offset(rows)))


def _display(line: LogLine) -> str:
    return f"{line.ts} {line.text}" if line.ts else line.text
