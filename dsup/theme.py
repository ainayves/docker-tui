"""Palette et symboles centralisés (tons ANSI 256 de la maquette).

Aucune couleur de fond n'est peinte : la sélection, les barres et les
bandeaux de confirmation utilisent la vidéo inversée, tout le reste hérite
du thème du terminal. Les accents colorés dans les barres composent leur
couleur avec la vidéo inversée.
"""

from __future__ import annotations

from rich.style import Style

from dsup.docker import ContainerState

GREEN = "color(71)"
ORANGE = "color(172)"
RED = "color(167)"
BLUE = "color(67)"
GREY = "color(245)"
DIM = "color(240)"

TEXT = Style()
S_GREEN = Style(color=GREEN)
S_ORANGE = Style(color=ORANGE)
S_RED = Style(color=RED)
S_BLUE = Style(color=BLUE)
S_GREY = Style(color=GREY)
S_DIM = Style(color=DIM)
S_BOLD = Style(bold=True)
S_FAINT = Style(dim=True)
REVERSE = Style(reverse=True)

# Barres haut/bas : vidéo inversée uniforme ; bandeaux d'alerte : rouge/bleu inversé.
BAR = Style(reverse=True)
CONFIRM_BAR = Style(color=RED, reverse=True)
PROMPT_BAR = Style(color=BLUE, reverse=True)

SPINNER = "⣾⣽⣻⢿⡿⣟⣯⣷"

_STATE_TEXT: dict[ContainerState, tuple[str, Style]] = {
    ContainerState.RUNNING: ("● up", S_GREEN),
    ContainerState.RESTARTING: ("↻ restart", S_ORANGE),
    ContainerState.PAUSED: ("◌ paused", S_GREY),
    ContainerState.CREATED: ("◌ created", S_GREY),
    ContainerState.EXITED: ("◌ exited", S_GREY),
}


def state_display(
    state: ContainerState, exit_code: int | None, pending: str | None = None
) -> tuple[str, Style]:
    """Cellule ÉTAT : symbole + mot + couleur ; l'action en cours l'emporte."""
    if pending is not None:
        return (f"↻ {pending}…", S_ORANGE)
    if state is ContainerState.ERROR:
        code = "?" if exit_code is None else str(exit_code)
        return (f"✖ err {code}", S_RED)
    return _STATE_TEXT[state]


def health_display(health: str) -> tuple[str, Style]:
    """Affichage de l'état de santé dans le panneau détail."""
    if health == "healthy":
        return ("● healthy", S_GREEN)
    if health == "unhealthy":
        return ("✖ unhealthy", S_RED)
    return (f"↻ {health}", S_ORANGE)
