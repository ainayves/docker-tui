"""Tests du presse-papiers : choix de l'outil natif, repli, aucun vrai processus."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from dsup import clipboard


def test_macos_utilise_pbcopy() -> None:
    assert clipboard.native_commands({}, "darwin") == [["pbcopy"]]


def test_linux_wayland_puis_x11() -> None:
    env = {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}
    assert [c[0] for c in clipboard.native_commands(env, "linux")] == [
        "wl-copy",
        "xclip",
        "xsel",
    ]


def test_linux_sans_affichage_aucun_outil() -> None:
    assert clipboard.native_commands({}, "linux") == []


@pytest.mark.parametrize("variable", ["SSH_CONNECTION", "SSH_TTY"])
def test_session_ssh_ecarte_l_outil_natif(variable: str) -> None:
    """Dans une session SSH, pbcopy écrirait dans le presse-papiers du serveur."""
    assert clipboard.native_commands({variable: "x"}, "darwin") == []


def _outils(
    monkeypatch: pytest.MonkeyPatch,
    commandes: list[list[str]],
    presents: set[str],
    codes: dict[str, int],
) -> list[tuple[list[str], Any]]:
    appels: list[tuple[list[str], Any]] = []
    monkeypatch.setattr(clipboard, "native_commands", lambda: commandes)
    monkeypatch.setattr(
        "dsup.clipboard.shutil.which", lambda nom: nom if nom in presents else None
    )

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        appels.append((command, kwargs))
        return subprocess.CompletedProcess(command, codes.get(command[0], 0))

    monkeypatch.setattr("dsup.clipboard.subprocess.run", run)
    return appels


def test_copie_native_transmet_le_texte_en_utf8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    appels = _outils(monkeypatch, [["pbcopy"]], {"pbcopy"}, {})

    assert clipboard.copy_native("café ✓") is True
    (command, kwargs), = appels
    assert command == ["pbcopy"]
    assert kwargs["input"] == "café ✓".encode()
    assert kwargs["env"]["LC_CTYPE"] == "UTF-8"
    # Pas de tube en sortie : un fils xclip le garderait ouvert.
    assert kwargs["stdout"] is subprocess.DEVNULL


def test_outil_absent_ou_en_echec_passe_au_suivant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commandes = [["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel"]]
    appels = _outils(monkeypatch, commandes, {"xclip", "xsel"}, {"xclip": 1})

    assert clipboard.copy_native("x") is True
    assert [c[0] for c, _ in appels] == ["xclip", "xsel"]


def test_aucun_outil_utilisable(monkeypatch: pytest.MonkeyPatch) -> None:
    _outils(monkeypatch, [["pbcopy"]], set(), {})
    assert clipboard.copy_native("x") is False


def test_delai_depasse_compte_comme_echec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clipboard, "native_commands", lambda: [["xclip"]])
    monkeypatch.setattr("dsup.clipboard.shutil.which", lambda nom: nom)

    def run(command: list[str], **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(command, clipboard.TIMEOUT_S)

    monkeypatch.setattr("dsup.clipboard.subprocess.run", run)
    assert clipboard.copy_native("x") is False
