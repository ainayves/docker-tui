"""Presse-papiers système : outil natif d'abord, OSC 52 en dernier recours.

dsup tourne sur le poste de l'utilisateur — le mode remote passe par un
tunnel, l'interface reste locale —, donc l'outil natif (pbcopy, wl-copy,
xclip, xsel) atteint le bon presse-papiers et confirme la copie par son code
de retour. OSC 52 confie la copie au terminal sans retour possible :
Terminal.app l'ignore, iTerm2 la refuse tant que l'option n'est pas cochée.
Elle ne passe donc qu'en second, et en premier seulement quand dsup tourne
lui-même dans une session SSH, où l'outil natif écrirait dans le
presse-papiers du serveur.

L'appel OSC 52 lui-même reste côté application (`App.copy_to_clipboard`) :
il écrit sur le terminal, ce que seul le thread de l'interface peut faire.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping

TIMEOUT_S = 2.0


def native_commands(
    env: Mapping[str, str] | None = None, platform: str | None = None
) -> list[list[str]]:
    """Outils candidats, dans l'ordre d'essai ; vide si aucun ne convient."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if env.get("SSH_CONNECTION") or env.get("SSH_TTY"):
        return []
    if platform == "darwin":
        return [["pbcopy"]]
    commands: list[list[str]] = []
    if env.get("WAYLAND_DISPLAY"):
        commands.append(["wl-copy"])
    if env.get("DISPLAY"):
        commands.append(["xclip", "-selection", "clipboard"])
        commands.append(["xsel", "--clipboard", "--input"])
    return commands


def copy_native(text: str) -> bool:
    """Copie par le premier outil natif qui réussit ; False si aucun.

    Bloquant (sous-processus) : à appeler hors du thread de l'interface.
    """
    data = text.encode("utf-8")
    # pbcopy décode son entrée selon la locale : sans locale UTF-8 (lancement
    # hors shell de connexion), les accents arriveraient en mojibake.
    env = {**os.environ, "LC_CTYPE": "UTF-8"}
    for command in native_commands():
        if shutil.which(command[0]) is None:
            continue
        try:
            # Sorties vers /dev/null, pas en tube : xclip et wl-copy laissent
            # un processus fils servir la sélection, qui garderait le tube
            # ouvert et ferait attendre run() jusqu'au délai.
            result = subprocess.run(
                command,
                input=data,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                timeout=TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            return True
    return False
