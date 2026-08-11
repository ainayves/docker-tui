"""Table des conteneurs : colonnes de la maquette, sélection stable, états pleins écran."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from rich.cells import cell_len
from rich.style import Style
from textual.strip import Strip
from textual.widget import Widget

from dsup import theme
from dsup.docker import ContainerInfo, ContainerState
from dsup.stats import ContainerStats
from dsup.widgets import Piece, fit, format_bytes, format_cpu, format_duration, strip_from

_STATE_W = 12
_NAME_W = 15
_CPU_W = 6
_RAM_W = 6
_PORTS_W = 8
_UP_W = 5


@dataclass(slots=True)
class Placeholder:
    """Message plein écran affiché à la place de la liste (vide, panne, connexion)."""

    rows: list[list[Piece]]


class ContainerTable(Widget):
    """Liste des conteneurs, rendue ligne à ligne (aucun fond peint)."""

    DEFAULT_CSS = "ContainerTable { width: 1fr; height: 1fr; }"

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[ContainerInfo] = []
        self._stats: dict[str, ContainerStats] = {}
        self._pending: dict[str, str] = {}
        self._placeholder: Placeholder | None = None
        self._selected_id: str | None = None
        self._selected_index = 0
        self._offset = 0

    # ------------------------------------------------------------- API écran

    @property
    def selected(self) -> ContainerInfo | None:
        for info in self._rows:
            if info.id == self._selected_id:
                return info
        return None

    def update_rows(
        self,
        rows: Sequence[ContainerInfo],
        stats: Mapping[str, ContainerStats],
        pending: Mapping[str, str],
    ) -> None:
        self._rows = list(rows)
        self._stats = dict(stats)
        self._pending = dict(pending)
        ids = [c.id for c in self._rows]
        if self._selected_id in ids:
            self._selected_index = ids.index(self._selected_id)
        elif self._rows:
            # Le conteneur sélectionné a disparu : on garde la position.
            self._selected_index = min(self._selected_index, len(ids) - 1)
            self._selected_id = ids[self._selected_index]
        else:
            self._selected_id = None
            self._selected_index = 0
        self._ensure_visible()
        self.refresh()

    def set_placeholder(self, placeholder: Placeholder | None) -> None:
        self._placeholder = placeholder
        self.refresh()

    def move(self, delta: int) -> None:
        if not self._rows:
            return
        self._selected_index = max(0, min(self._selected_index + delta, len(self._rows) - 1))
        self._selected_id = self._rows[self._selected_index].id
        self._ensure_visible()
        self.refresh()

    def select_first(self) -> None:
        self.move(-len(self._rows))

    def select_last(self) -> None:
        self.move(len(self._rows))

    # ---------------------------------------------------------------- rendu

    def render_line(self, y: int) -> Strip:
        width = max(1, self.size.width)
        if self._placeholder is not None:
            return self._placeholder_line(y, width)
        if not self._rows:
            return strip_from([("~", theme.S_DIM)], width)
        display = self._display_rows()
        if y == 0:
            return self._header_line(width)
        index = self._offset + y - 1
        if 0 <= index < len(display):
            kind, info = display[index]
            if kind == "detail":
                return self._detail_line(info, width)
            return self._row_line(info, width)
        return strip_from([("~", theme.S_DIM)], width)

    def _display_rows(self) -> list[tuple[str, ContainerInfo]]:
        display: list[tuple[str, ContainerInfo]] = []
        for info in self._rows:
            display.append(("row", info))
            if info.state is ContainerState.RESTARTING:
                display.append(("detail", info))
        return display

    def _ensure_visible(self) -> None:
        height = self.size.height
        if height <= 1:
            return
        display = self._display_rows()
        position = next(
            (
                i
                for i, (kind, info) in enumerate(display)
                if kind == "row" and info.id == self._selected_id
            ),
            0,
        )
        visible = height - 1
        if position < self._offset:
            self._offset = position
        elif position >= self._offset + visible:
            self._offset = position - visible + 1
        self._offset = max(0, min(self._offset, max(0, len(display) - visible)))

    def _columns(self, width: int) -> tuple[int, int]:
        fixed = 2 + _STATE_W + _NAME_W + _CPU_W + _RAM_W + 1 + _PORTS_W + 1 + _UP_W
        return _NAME_W, max(10, width - fixed)

    def _header_line(self, width: int) -> Strip:
        name_w, image_w = self._columns(width)
        text = (
            "  "
            + fit("ÉTAT", _STATE_W)
            + fit("NOM", name_w)
            + fit("IMAGE", image_w)
            + fit("CPU", _CPU_W, align="right")
            + fit("RAM", _RAM_W, align="right")
            + " "
            + fit("PORTS", _PORTS_W)
            + " "
            + fit("UP", _UP_W, align="right")
        )
        return strip_from([(text, theme.S_GREY)], width)

    def _row_line(self, info: ContainerInfo, width: int) -> Strip:
        name_w, image_w = self._columns(width)
        selected = info.id == self._selected_id
        pending = self._pending.get(info.id)
        state_text, state_style = theme.state_display(info.state, info.exit_code, pending)
        stats = self._stats.get(info.id)
        show_stats = info.state is ContainerState.RUNNING and pending is None
        cpu = format_cpu(stats.cpu_percent) if show_stats and stats else "—"
        ram = format_bytes(stats.mem_used_bytes) if show_stats and stats else "—"
        ports = ",".join(info.ports) if info.ports else "—"
        if show_stats and info.started_at is not None:
            delta = datetime.now(timezone.utc) - info.started_at
            uptime = format_duration(delta.total_seconds())
        else:
            uptime = "—"
        pieces: list[Piece] = [
            ("▸ " if selected else "  ", theme.TEXT),
            (fit(state_text, _STATE_W), state_style),
            (fit(info.name, name_w), theme.TEXT),
            (fit(info.image, image_w), theme.S_GREY),
            (fit(cpu, _CPU_W, align="right"), theme.TEXT),
            (fit(ram, _RAM_W, align="right"), theme.TEXT),
            (" ", theme.TEXT),
            (fit(ports, _PORTS_W), theme.TEXT),
            (" ", theme.TEXT),
            (fit(uptime, _UP_W, align="right"), theme.S_GREY),
        ]
        overlay = theme.REVERSE if selected else None
        return strip_from(pieces, width, overlay=overlay)

    def _detail_line(self, info: ContainerInfo, width: int) -> Strip:
        detail = f"redémarre · essai {info.restart_count}"
        if info.exit_code is not None:
            detail += f" · dernier code : {info.exit_code}"
        return strip_from([("  └ ", theme.S_ORANGE), (detail, theme.S_GREY)], width)

    def _placeholder_line(self, y: int, width: int) -> Strip:
        assert self._placeholder is not None
        rows = self._placeholder.rows
        height = max(1, self.size.height)
        start = max(1, height // 2 - (len(rows) + 1) // 2)
        index = y - start
        if 0 <= index < len(rows):
            pieces = rows[index]
            content = sum(cell_len(text) for text, _ in pieces)
            pad = " " * max(0, (width - content) // 2)
            return strip_from([(pad, theme.TEXT), *pieces], width)
        return strip_from([("~", theme.S_DIM)], width)


def message_placeholder(
    icon: str,
    icon_style: Style,
    title: str,
    lines: Sequence[str] = (),
    footer: str = "",
) -> Placeholder:
    """Construit l'état plein écran de la maquette : icône + titre centrés."""
    rows: list[list[Piece]] = [[(f"{icon}  ", icon_style), (title, theme.TEXT)]]
    rows.extend([(line, theme.S_GREY)] for line in lines if line)
    if footer:
        rows.append([])
        rows.append([(footer, theme.S_GREY)])
    return Placeholder(rows=rows)
