"""Panneau de logs : tampon circulaire borné, suivi, recherche, wrap."""

from __future__ import annotations

import re
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
    """Tampon circulaire borné, alimenté par morceaux bruts du flux Docker."""

    def __init__(self, maxlen: int = 2000) -> None:
        self._lines: deque[LogLine] = deque(maxlen=maxlen)
        self._partial = b""
        self.version = 0

    def feed(self, chunk: bytes) -> None:
        """Découpe le flux en lignes ; l'éventuel reste attend le prochain chunk."""
        data = self._partial + chunk
        parts = data.split(b"\n")
        self._partial = parts.pop()
        for raw in parts:
            line = raw.decode("utf-8", errors="replace").rstrip("\r")
            ts = ""
            match = _TS_PREFIX_RE.match(line)
            if match is not None:
                ts = match.group(2)
                line = line[match.end() :]
            self._lines.append(LogLine(ts, line, _classify(line)))
        if parts:
            self.version += 1

    def __len__(self) -> int:
        return len(self._lines)

    def snapshot(self) -> tuple[LogLine, ...]:
        return tuple(self._lines)


class LogView(Widget):
    """Affiche un LogBuffer ; défiler vers le haut coupe le suivi, G/f le reprennent."""

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
        self._lines: tuple[LogLine, ...] = ()
        self._vrows: list[tuple[int, int, int]] = []

    # ------------------------------------------------------------- API écran

    def attach(self, buffer: LogBuffer) -> None:
        self._buffer = buffer
        self.refresh()

    def on_new_lines(self) -> None:
        if self._query:
            self._matches = self._compute_matches()
        self.refresh()

    def scroll_by(self, delta: int) -> None:
        rows = self._rows()
        height = max(1, self.size.height)
        max_offset = max(0, len(rows) - height)
        current = max_offset if self.follow else min(self._offset, max_offset)
        self._offset = max(0, min(current + delta, max_offset))
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
            height = max(1, self.size.height)
            top_offset = (
                max(0, len(rows) - height) if self.follow else min(self._offset, len(rows))
            )
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

    # ---------------------------------------------------------------- rendu

    def render_line(self, y: int) -> Strip:
        width = max(1, self.size.width)
        rows = self._rows()
        height = max(1, self.size.height)
        max_offset = max(0, len(rows) - height)
        offset = max_offset if self.follow else min(self._offset, max_offset)
        index = offset + y
        if index >= len(rows):
            return strip_from([("~", theme.S_DIM)], width)
        line_index, start, end = rows[index]
        return strip_from(self._slice_pieces(line_index, start, end), width)

    def _rows(self) -> list[tuple[int, int, int]]:
        buffer = self._buffer
        if buffer is None:
            return []
        width = max(1, self.size.width)
        key = (buffer.version, width, self.wrap)
        if key != self._cache_key:
            self._cache_key = key
            self._lines = buffer.snapshot()
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

    def _slice_pieces(self, line_index: int, start: int, end: int) -> list[Piece]:
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
            if any(ha <= a and b <= hb for ha, hb in highlights):
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
        self._offset = max(0, min(row - height // 2, max(0, len(rows) - height)))


def _display(line: LogLine) -> str:
    return f"{line.ts} {line.text}" if line.ts else line.text
