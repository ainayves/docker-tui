"""Tests du tunnel SSH : cibles, chemins, commande construite, cycle de vie.

Aucun test ne sort sur le réseau : `subprocess.run` est systématiquement
remplacé. Une suite dont la durée explose signalerait une vraie connexion.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from dsup import tunnel
from dsup.tunnel import Target, TunnelError, normalize_target


@pytest.fixture(autouse=True)
def _racine_temporaire(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isole les sockets : jamais de ~/.dsup réel pendant les tests."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


class _SshFactice:
    """Remplace subprocess.run et retient les commandes lancées.

    Simule aussi l'effet observable d'un ssh réussi : la création de la socket
    locale demandée par `-L`. Sans cela, `open()` — qui vérifie à juste titre
    que la socket existe bien en sortie — rejetterait le succès simulé.
    """

    def __init__(
        self, code: int = 0, stderr: str = "", cree_socket: bool = True
    ) -> None:
        self.code = code
        self.stderr = stderr
        self.cree_socket = cree_socket
        self.commandes: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.commandes.append(command)
        if self.code == 0 and self.cree_socket and "-L" in command:
            forward = command[command.index("-L") + 1]
            Path(forward.split(":", 1)[0]).touch()
        return subprocess.CompletedProcess(command, self.code, "", self.stderr)

    @property
    def derniere(self) -> list[str]:
        return self.commandes[-1]


@pytest.mark.parametrize(
    ("raw", "attendu"),
    [
        ("prod", Target(host="prod")),
        ("root@10.0.0.1", Target(host="10.0.0.1", user="root")),
        ("root@10.0.0.1:2222", Target(host="10.0.0.1", user="root", port=2222)),
        ("  prod  ", Target(host="prod")),
        ("ssh://root@10.0.0.1", Target(host="10.0.0.1", user="root")),
    ],
)
def test_normalize_target(raw: str, attendu: Target) -> None:
    assert normalize_target(raw) == attendu


def test_normalize_target_refuse_les_autres_transports() -> None:
    """Un tcp:// ne se tunnelise pas : le dire plutôt que de bâtir un ssh absurde."""
    with pytest.raises(TunnelError) as exc:
        normalize_target("tcp://10.0.0.1:2375")
    assert "non géré" in str(exc.value)


def test_normalize_target_refuse_le_vide() -> None:
    with pytest.raises(TunnelError):
        normalize_target("  ")


@pytest.mark.parametrize(
    ("cible", "attendu"),
    [
        (Target(host="h"), ["--", "h"]),
        (Target(host="h", user="root"), ["-l", "root", "--", "h"]),
        (Target(host="h", user="root", port=22), ["-l", "root", "-p", "22", "--", "h"]),
    ],
)
def test_ssh_args(cible: Target, attendu: list[str]) -> None:
    assert cible.ssh_args() == attendu


def test_socket_courte_et_stable() -> None:
    """Chemin court (limite ~104 caractères) et identique d'un appel à l'autre.

    La stabilité est ce qui permet au lancement suivant de retrouver le
    tunnel et de démarrer sans authentifier.
    """
    cible = Target(host="164.68.100.31", user="root")
    premier = tunnel.socket_path(cible)
    assert premier == tunnel.socket_path(cible)
    # Mesuré depuis le dossier personnel : c'est la part que dsup contrôle.
    # Un nom d'hôte long ne doit jamais allonger le chemin, d'où le condensat.
    assert len(str(premier.relative_to(Path.home()))) < 30
    assert premier != tunnel.socket_path(Target(host="autre", user="root"))
    # Forwarding et contrôle ne doivent jamais partager le même fichier.
    assert premier != tunnel.control_path(cible)


def test_dossier_prive() -> None:
    """Cette socket vaut un accès root sur le serveur : 0700 obligatoire."""
    dossier = tunnel.runtime_dir()
    assert dossier.stat().st_mode & 0o077 == 0


def test_dossier_preexistant_est_reserre(tmp_path: Path) -> None:
    """Un ~/.dsup trop permissif hérité d'avant doit être corrigé, pas accepté."""
    (tmp_path / ".dsup").mkdir(mode=0o755)
    assert tunnel.runtime_dir().stat().st_mode & 0o077 == 0


def test_docker_host_pointe_sur_la_socket() -> None:
    cible = Target(host="h", user="root")
    assert tunnel.docker_host(cible) == f"unix://{tunnel.socket_path(cible)}"


def test_commande_sans_execution_distante(monkeypatch: pytest.MonkeyPatch) -> None:
    """`-N` : aucune commande sur le serveur, donc pas de CLI docker requis.

    C'est la propriété qui distingue ce transport de `docker system
    dial-stdio`, et la raison d'être du module.
    """
    faux = _SshFactice()
    monkeypatch.setattr("dsup.tunnel.subprocess.run", faux)
    cible = Target(host="10.0.0.1", user="root")
    tunnel.socket_path(cible).touch()  # ssh est simulé : on crée la socket

    tunnel.open(cible)

    commande = faux.derniere
    assert "-N" in commande
    # Rien n'est appendu après la destination : ssh n'exécute donc aucune
    # commande sur le serveur, contrairement à `… hôte 'docker system
    # dial-stdio'`. Le CLI docker distant devient inutile.
    assert commande[-1] == "10.0.0.1"
    assert "dial-stdio" not in " ".join(commande)
    assert f"{tunnel.socket_path(cible)}:/var/run/docker.sock" in commande


def test_commande_options_de_robustesse(monkeypatch: pytest.MonkeyPatch) -> None:
    faux = _SshFactice()
    monkeypatch.setattr("dsup.tunnel.subprocess.run", faux)
    cible = Target(host="h")
    tunnel.socket_path(cible).touch()

    tunnel.open(cible)

    jointe = " ".join(faux.derniere)
    assert "ExitOnForwardFailure=yes" in jointe
    assert "StreamLocalBindUnlink=yes" in jointe
    assert "ServerAliveInterval=15" in jointe
    assert "ServerAliveCountMax=3" in jointe


def test_interactif_autorise_la_saisie(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avant l'interface : ssh doit pouvoir demander un mot de passe."""
    faux = _SshFactice()
    monkeypatch.setattr("dsup.tunnel.subprocess.run", faux)
    cible = Target(host="h")
    tunnel.socket_path(cible).touch()

    tunnel.open(cible, interactive=True)

    assert "BatchMode=yes" not in " ".join(faux.derniere)


def test_non_interactif_impose_batchmode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invariant décisif : une relance automatique ne doit rien demander.

    Ces relances surviennent pendant que l'interface tient le terminal. Sans
    BatchMode, chaque tentative réclamerait un mot de passe sur /dev/tty et
    volerait le clavier à Textual — le défaut qui a motivé cette refonte.
    """
    faux = _SshFactice()
    monkeypatch.setattr("dsup.tunnel.subprocess.run", faux)
    cible = Target(host="h")
    tunnel.socket_path(cible).touch()

    tunnel.open(cible, interactive=False)

    assert "BatchMode=yes" in " ".join(faux.derniere)


def test_socket_distante_personnalisee(monkeypatch: pytest.MonkeyPatch) -> None:
    """Docker rootless expose sa socket ailleurs."""
    faux = _SshFactice()
    monkeypatch.setattr("dsup.tunnel.subprocess.run", faux)
    cible = Target(host="h")
    tunnel.socket_path(cible).touch()

    tunnel.open(cible, remote_socket="/run/user/1000/docker.sock")

    assert f"{tunnel.socket_path(cible)}:/run/user/1000/docker.sock" in faux.derniere


def test_echec_ssh_leve_avec_conseil(monkeypatch: pytest.MonkeyPatch) -> None:
    faux = _SshFactice(code=255, stderr="root@h: Permission denied (publickey,password).")
    monkeypatch.setattr("dsup.tunnel.subprocess.run", faux)

    with pytest.raises(TunnelError) as exc:
        tunnel.open(Target(host="h", user="root"), interactive=False)
    assert "ssh-copy-id root@h" in exc.value.hint


def test_forwarding_refuse_par_le_serveur(monkeypatch: pytest.MonkeyPatch) -> None:
    faux = _SshFactice(code=255, stderr="administratively prohibited: open failed")
    monkeypatch.setattr("dsup.tunnel.subprocess.run", faux)

    with pytest.raises(TunnelError) as exc:
        tunnel.open(Target(host="h"), interactive=False)
    assert "AllowStreamLocalForwarding" in exc.value.hint


def test_socket_absente_apres_ssh(monkeypatch: pytest.MonkeyPatch) -> None:
    """ssh a rendu 0 mais rien n'a été créé : le dire, ne pas laisser croire."""
    monkeypatch.setattr("dsup.tunnel.subprocess.run", _SshFactice(cree_socket=False))

    with pytest.raises(TunnelError) as exc:
        tunnel.open(Target(host="h"))
    assert "n'a pas créé la socket" in str(exc.value)


def test_ensure_reutilise_un_tunnel_vivant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Second lancement : aucun ssh, donc démarrage immédiat et sans question."""
    faux = _SshFactice()
    monkeypatch.setattr("dsup.tunnel.subprocess.run", faux)
    monkeypatch.setattr(tunnel, "is_alive", lambda _: True)
    cible = Target(host="h")

    assert tunnel.ensure(cible) == tunnel.socket_path(cible)
    assert faux.commandes == []


def test_is_alive_faux_sans_controle() -> None:
    assert tunnel.is_alive(Target(host="jamais-ouvert")) is False


def test_close_sans_tunnel(monkeypatch: pytest.MonkeyPatch) -> None:
    assert tunnel.close(Target(host="h")) is False


def test_close_ferme_et_nettoie(monkeypatch: pytest.MonkeyPatch) -> None:
    faux = _SshFactice()
    monkeypatch.setattr("dsup.tunnel.subprocess.run", faux)
    cible = Target(host="h")
    tunnel.control_path(cible).touch()
    tunnel.socket_path(cible).touch()

    assert tunnel.close(cible) is True
    assert "exit" in faux.derniere
    assert not tunnel.socket_path(cible).exists()
    assert not tunnel.control_path(cible).exists()


def test_env_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCKER_HOST", "ssh://root@10.0.0.1")
    assert tunnel.env_target() == Target(host="10.0.0.1", user="root")
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    assert tunnel.env_target() is None
    monkeypatch.delenv("DOCKER_HOST")
    assert tunnel.env_target() is None
