"""Tunnel SSH : une connexion permanente, rien à installer sur le serveur.

Le mode remote pourrait s'appuyer sur `DOCKER_HOST=ssh://…`, que docker-py
traduit en `ssh -- hôte 'docker system dial-stdio'`. Trois défauts en
découlent, et ce module existe pour les éviter tous les trois :

1. `dial-stdio` est une commande exécutée **sur le serveur** : le CLI docker y
   devient obligatoire. Ici, on ne touche qu'à la socket — aucune commande
   n'est lancée à distance.
2. docker-py ouvre une connexion SSH par requête, et chacune s'authentifie
   séparément. Sur un hôte en mot de passe, les prompts surgissent pendant que
   l'interface tient le terminal, s'entrelacent et lui volent le clavier.
3. Le pool SSH de docker-py est plafonné à 9 connexions (DEFAULT_NUM_POOLS_SSH)
   contre 25 en transport unix ; les flux de logs et de stats l'épuisent.

Le principe tient en deux lignes :

    ssh -fN -L ~/.dsup/<id>.sock:/var/run/docker.sock root@hôte
    DOCKER_HOST=unix://~/.dsup/<id>.sock

docker-py ne voit plus qu'une socket Unix locale et n'invoque plus jamais
`ssh`. Une seule authentification, faite avant l'affichage, dans un terminal
normal — donc sans limite de temps et sans risque de prompt sous l'interface.

Le tunnel survit à dsup : le lancement suivant réutilise la socket et démarre
sans rien demander. `close()` y met fin.

Attention : cette socket locale donne un accès complet au daemon distant, ce
qui équivaut à root sur ce serveur. D'où le dossier en 0700.
"""

from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

DEFAULT_REMOTE_SOCKET = "/var/run/docker.sock"

# Détection d'un lien mort : trois sondes espacées de 15 s. Sans cela, un
# tunnel dont le réseau a disparu (veille, VPN, changement de wifi) resterait
# ouvert côté client et les requêtes attendraient indéfiniment.
_ALIVE_INTERVAL_S = 15
_ALIVE_COUNT_MAX = 3

# Borne l'établissement TCP seulement. La saisie du mot de passe, elle, n'est
# jamais limitée : c'est tout l'intérêt d'authentifier avant l'interface.
_CONNECT_TIMEOUT_S = 15

# Survie de la connexion maîtresse après sa dernière utilisation. Elle sert au
# cycle de vie (`-O check`, `-O exit`), pas au multiplexage.
_CONTROL_PERSIST = "10m"


class TunnelError(Exception):
    """Échec d'ouverture ou de fermeture du tunnel, avec conseil de réparation."""

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


@dataclass(frozen=True)
class Target:
    """Destination SSH : ce qu'il faut pour bâtir une ligne de commande ssh."""

    host: str
    user: str = ""
    port: int = 0

    def __str__(self) -> str:
        base = f"{self.user}@{self.host}" if self.user else self.host
        return f"{base}:{self.port}" if self.port else base

    @property
    def destination(self) -> str:
        """Forme `user@hôte` destinée aux messages et à ssh-copy-id."""
        return f"{self.user}@{self.host}" if self.user else self.host

    def ssh_args(self) -> list[str]:
        """Arguments de destination, dans l'ordre attendu par ssh."""
        args: list[str] = []
        if self.user:
            args += ["-l", self.user]
        if self.port:
            args += ["-p", str(self.port)]
        return args + ["--", self.host]


def normalize_target(raw: str) -> Target:
    """Interprète une cible de ligne de commande.

    Les formes sans schéma sont du SSH — c'est le seul transport qu'on écrit à
    la main :

        prod              -> Target(host="prod")
        root@10.0.0.1     -> Target(host="10.0.0.1", user="root")
        root@10.0.0.1:22  -> Target(host="10.0.0.1", user="root", port=22)
        ssh://root@hôte   -> idem, le schéma est simplement toléré
    """
    target = raw.strip()
    if not target:
        raise TunnelError("cible vide")
    if "://" in target:
        scheme, _, _ = target.partition("://")
        if scheme != "ssh":
            raise TunnelError(
                f"transport {scheme}:// non géré par le tunnel",
                hint="dsup n'ouvre de tunnel que vers du SSH ; "
                "pour un autre transport, posez DOCKER_HOST vous-même",
            )
    else:
        target = f"ssh://{target}"

    parsed = urllib.parse.urlparse(target)
    if not parsed.hostname:
        raise TunnelError(f"cible illisible : {raw}")
    return Target(
        host=parsed.hostname,
        user=parsed.username or "",
        port=parsed.port or 0,
    )


def runtime_dir() -> Path:
    """Dossier des sockets, créé au besoin en 0700.

    Le mode importe autant que l'existence : quiconque atteint ces sockets
    pilote le daemon distant, donc dispose de l'équivalent de root sur le
    serveur.
    """
    path = Path.home() / ".dsup"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    # mkdir n'ajuste rien si le dossier préexiste avec des droits trop larges.
    path.chmod(0o700)
    return path


def _digest(target: Target) -> str:
    return hashlib.sha256(str(target).encode()).hexdigest()[:8]


def socket_path(target: Target) -> Path:
    """Chemin de la socket locale, stable d'un lancement à l'autre.

    La stabilité est ce qui rend le tunnel réutilisable : au lancement
    suivant, dsup retrouve la socket et démarre sans authentifier.

    Le chemin doit rester court — une socket Unix est plafonnée autour de 104
    caractères sur macOS — d'où un condensat de 8 caractères plutôt que le nom
    d'hôte.
    """
    return runtime_dir() / f"{_digest(target)}.sock"


def control_path(target: Target) -> Path:
    """Socket de contrôle, distincte de celle du forwarding."""
    return runtime_dir() / f"{_digest(target)}.ctl"


def docker_host(target: Target) -> str:
    """Valeur de DOCKER_HOST correspondant au tunnel de cette cible."""
    return f"unix://{socket_path(target)}"


def _ssh() -> str:
    return "ssh"


def is_alive(target: Target) -> bool:
    """Un tunnel utilisable existe-t-il déjà pour cette cible ?

    Deux niveaux, du moins cher au plus sûr : la connexion maîtresse
    répond-elle, puis la socket de forwarding accepte-t-elle une connexion.
    Le premier seul ne suffit pas — la maîtresse peut survivre à un forward
    perdu — et le second seul laisserait passer un fichier résiduel.
    """
    control = control_path(target)
    if not control.exists():
        return False
    probe = subprocess.run(
        [_ssh(), "-o", f"ControlPath={control}", "-O", "check", *target.ssh_args()],
        capture_output=True,
    )
    if probe.returncode != 0:
        return False

    local = socket_path(target)
    if not local.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe_sock:
            probe_sock.settimeout(2.0)
            probe_sock.connect(str(local))
    except OSError:
        return False
    return True


def _build_command(
    target: Target, remote_socket: str, *, interactive: bool
) -> list[str]:
    command = [
        _ssh(),
        "-f",  # passe en arrière-plan une fois l'authentification faite
        "-N",  # aucune commande distante : c'est ce qui dispense le serveur
        #        d'avoir le CLI docker, contrairement à dial-stdio
        "-o", "ExitOnForwardFailure=yes",
        "-o", "StreamLocalBindUnlink=yes",
        "-o", f"ServerAliveInterval={_ALIVE_INTERVAL_S}",
        "-o", f"ServerAliveCountMax={_ALIVE_COUNT_MAX}",
        "-o", f"ConnectTimeout={_CONNECT_TIMEOUT_S}",
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={control_path(target)}",
        "-o", f"ControlPersist={_CONTROL_PERSIST}",
    ]
    if not interactive:
        # Relance automatique, pendant que l'interface tient le terminal :
        # aucune question ne doit pouvoir être posée. Sans cette garde, chaque
        # tentative réclamerait un mot de passe sur /dev/tty et volerait le
        # clavier à Textual.
        command += ["-o", "BatchMode=yes"]
    command += ["-L", f"{socket_path(target)}:{remote_socket}"]
    return command + target.ssh_args()


def open(
    target: Target,
    *,
    remote_socket: str = DEFAULT_REMOTE_SOCKET,
    interactive: bool = True,
) -> Path:
    """Ouvre le tunnel et renvoie la socket locale.

    `interactive=True` hérite des flux du terminal : mot de passe, passphrase
    et confirmation d'hôte inconnu fonctionnent, sans limite de durée. À
    n'utiliser qu'avant le démarrage de l'interface.

    Lève `TunnelError` si ssh échoue — authentification refusée, hôte
    injoignable, ou socket distante absente.
    """
    local = socket_path(target)
    # StreamLocalBindUnlink couvre le cas normal, mais un fichier orphelin
    # laissé par un arrêt brutal ferait échouer le forward.
    if local.exists() and not is_alive(target):
        local.unlink(missing_ok=True)

    command = _build_command(target, remote_socket, interactive=interactive)
    try:
        result = subprocess.run(
            command,
            capture_output=not interactive,
            text=True,
        )
    except OSError as exc:
        raise TunnelError(f"impossible de lancer ssh : {exc}") from exc

    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        raison = detail[-1] if detail else f"ssh a échoué (code {result.returncode})"
        raise TunnelError(
            f"tunnel vers {target.destination} impossible : {raison}",
            hint=_failure_hint(target, raison),
        )
    if not local.exists():
        raise TunnelError(
            f"ssh n'a pas créé la socket {local}",
            hint=f"vérifiez que {remote_socket} existe sur {target.destination}",
        )
    return local


def _failure_hint(target: Target, raison: str) -> str:
    lowered = raison.lower()
    if "permission denied" in lowered or "publickey" in lowered:
        return (
            "installez une clé pour ne plus saisir de mot de passe : "
            f"ssh-copy-id {target.destination}"
        )
    if "host key" in lowered or "known_hosts" in lowered:
        return f"connectez-vous une fois à la main : ssh {target.destination}"
    if "administratively prohibited" in lowered or "forward" in lowered:
        return (
            "le serveur refuse le forwarding de socket ; "
            "vérifiez AllowStreamLocalForwarding dans sshd_config"
        )
    return f"vérifiez l'accès : ssh {target.destination}"


def ensure(
    target: Target,
    *,
    remote_socket: str = DEFAULT_REMOTE_SOCKET,
    interactive: bool = True,
) -> Path:
    """Renvoie un tunnel utilisable, en le rouvrant seulement si nécessaire.

    C'est le point d'entrée normal : au premier lancement il authentifie, aux
    suivants il constate que le tunnel tient et rend la main immédiatement.
    """
    if is_alive(target):
        return socket_path(target)
    return open(target, remote_socket=remote_socket, interactive=interactive)


def close(target: Target) -> bool:
    """Ferme le tunnel. Renvoie False s'il n'y en avait pas."""
    control = control_path(target)
    if not control.exists():
        socket_path(target).unlink(missing_ok=True)
        return False
    result = subprocess.run(
        [_ssh(), "-o", f"ControlPath={control}", "-O", "exit", *target.ssh_args()],
        capture_output=True,
    )
    socket_path(target).unlink(missing_ok=True)
    control.unlink(missing_ok=True)
    return result.returncode == 0


def env_target() -> Target | None:
    """Cible déduite d'un DOCKER_HOST en ssh://, s'il y en a un."""
    host = os.environ.get("DOCKER_HOST", "").strip()
    if not host.startswith("ssh://"):
        return None
    try:
        return normalize_target(host)
    except TunnelError:
        return None
