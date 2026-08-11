"""Barres haut/bas en vidéo inversée : touches, confirmation, message, saisie."""

from __future__ import annotations

from collections.abc import Sequence

from rich.style import Style
from textual.strip import Strip
from textual.widget import Widget

from dsup import theme
from dsup.widgets import Piece, bar_pieces, strip_from


class TopBar(Widget):
    """Barre du haut : contexte à gauche, fraîcheur/compteur à droite."""

    DEFAULT_CSS = "TopBar { height: 1; }"

    def __init__(self) -> None:
        super().__init__()
        self._left: list[Piece] = []
        self._right: list[Piece] = []

    def set_content(self, left: Sequence[Piece], right: Sequence[Piece] = ()) -> None:
        self._left = list(left)
        self._right = list(right)
        self.refresh()

    def render_line(self, y: int) -> Strip:
        width = max(1, self.size.width)
        pieces = bar_pieces(self._left, self._right, width)
        return strip_from(pieces, width, overlay=theme.BAR)


class BottomBar(Widget):
    """Barre du bas : touches par défaut, bandeaux confirmation/erreur/saisie."""

    DEFAULT_CSS = "BottomBar { height: 1; }"

    def __init__(self) -> None:
        super().__init__()
        self._left: list[Piece] = []
        self._right: list[Piece] = []
        self._overlay: Style = theme.BAR

    def set_content(
        self,
        left: Sequence[Piece],
        right: Sequence[Piece] = (),
        overlay: Style | None = None,
    ) -> None:
        self._left = list(left)
        self._right = list(right)
        self._overlay = overlay if overlay is not None else theme.BAR
        self.refresh()

    def show_keys(
        self,
        keys: Sequence[tuple[str, str]],
        right: Sequence[Piece] = (),
        prefix: Sequence[Piece] = (),
    ) -> None:
        left: list[Piece] = list(prefix)
        for key, label in keys:
            left.append((key, theme.S_BOLD))
            left.append((f" {label}  ", theme.S_FAINT))
        self.set_content(left, right)

    def show_confirm(self, action: str, name: str) -> None:
        left: list[Piece] = [
            (f"{action} {name} ?", theme.S_BOLD),
            ("  y confirmer · toute autre touche annule", theme.TEXT),
        ]
        self.set_content(left, (), overlay=theme.CONFIRM_BAR)

    def show_message(self, message: str, hint: str | None = None) -> None:
        # Pas de style dim ici : combiné à l'overlay coloré et au fond
        # "terminal default", le filtre de rendu de Textual ne sait pas le
        # convertir. Le bandeau reste monochrome inversé.
        left: list[Piece] = [("✖ ", theme.S_BOLD), (message, theme.TEXT)]
        if hint:
            left.append((f" — {hint}", theme.TEXT))
        self.set_content(left, (), overlay=theme.CONFIRM_BAR)

    def show_prompt(
        self, prefix: str, value: str, right: Sequence[Piece] = ()
    ) -> None:
        left: list[Piece] = [
            (prefix, theme.S_BOLD),
            (value, theme.TEXT),
            ("▏", theme.TEXT),
        ]
        self.set_content(left, right, overlay=theme.PROMPT_BAR)

    def render_line(self, y: int) -> Strip:
        width = max(1, self.size.width)
        pieces = bar_pieces(self._left, self._right, width)
        return strip_from(pieces, width, overlay=self._overlay)
