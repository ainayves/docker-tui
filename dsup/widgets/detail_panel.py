"""Panneau détail des grands terminaux : fiche du conteneur + fil de logs."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

from textual.strip import Strip
from textual.widget import Widget

from dsup import theme
from dsup.docker import ContainerInfo
from dsup.stats import ContainerStats
from dsup.widgets import Piece, format_ago, format_bytes, strip_from
from dsup.widgets.log_view import LogLine

_LEVEL_STYLES = (theme.TEXT, theme.S_ORANGE, theme.S_RED)


class DetailPanel(Widget):
    """Colonne droite (terminaux larges) : mêmes informations, zéro touche en plus."""

    DEFAULT_CSS = "DetailPanel { width: 40; height: 1fr; display: none; }"

    def __init__(self) -> None:
        super().__init__()
        self._head: list[list[Piece]] = []
        self._logs: Sequence[LogLine] = ()

    def set_data(
        self,
        info: ContainerInfo | None,
        stats: ContainerStats | None,
        log_lines: Sequence[LogLine],
        following: bool,
    ) -> None:
        self._head = self._build_head(info, stats, following)
        self._logs = tuple(log_lines)
        self.refresh()

    def _build_head(
        self,
        info: ContainerInfo | None,
        stats: ContainerStats | None,
        following: bool,
    ) -> list[list[Piece]]:
        rows: list[list[Piece]] = [[(" détail", theme.S_BOLD)], []]
        if info is None:
            rows.append([(" —", theme.S_GREY)])
            return rows

        def field(label: str, pieces: list[Piece]) -> None:
            rows.append([(f" {label:<9}", theme.S_GREY), *pieces])

        field("id", [(info.id[:12], theme.TEXT)])
        field("image", [(info.image, theme.TEXT)])
        if info.created_at is not None:
            age = (datetime.now(timezone.utc) - info.created_at).total_seconds()
            created = info.created_at.astimezone().strftime("%d-%m %H:%M")
            field("créé", [(f"{created} ({format_ago(age)})", theme.TEXT)])
        if info.restart_policy:
            field("restart", [(info.restart_policy, theme.TEXT)])
        if info.health:
            field("santé", [theme.health_display(info.health)])
        field("ports", [(",".join(info.ports) if info.ports else "—", theme.TEXT)])
        if stats is not None and stats.mem_used_bytes is not None:
            ram = f"{format_bytes(stats.mem_used_bytes)} / {format_bytes(stats.mem_limit_bytes)}"
            field("ram", [(ram, theme.TEXT)])
        rows.append([])
        follow_piece: Piece = (
            (" · suivi ●", theme.S_GREEN) if following else (" · suivi ○", theme.S_FAINT)
        )
        rows.append([(" logs", theme.S_BOLD), follow_piece])
        rows.append([(" " + "─" * max(0, self.size.width - 3), theme.S_DIM)])
        return rows

    def render_line(self, y: int) -> Strip:
        width = max(1, self.size.width)
        border: Piece = ("│", theme.S_DIM)
        if y < len(self._head):
            return strip_from([border, *self._head[y]], width)
        available = max(0, self.size.height - len(self._head))
        tail = list(self._logs)[-available:] if available else []
        index = y - len(self._head)
        if index < len(tail):
            line = tail[index]
            pieces: list[Piece] = [border, (" ", theme.TEXT)]
            if line.ts:
                pieces.append((f"{line.ts} ", theme.S_GREY))
            pieces.append((line.text, _LEVEL_STYLES[line.level]))
            return strip_from(pieces, width)
        return strip_from([border, ("~", theme.S_DIM)], width)
