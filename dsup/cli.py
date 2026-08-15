"""Point d'entrée : parsing des arguments et lancement de l'application.

Le daemon absent au lancement n'est pas une erreur fatale : l'application
démarre et affiche l'état dégradé avec nouvel essai automatique. Les erreurs
qui empêchent tout de même le démarrage sortent en message clair, sans
traceback.

L'ordre des opérations est ici la partie qui compte. Le tunnel SSH s'ouvre
*avant* `app.run()`, donc pendant que le terminal est encore normal : `ssh`
peut réclamer un mot de passe ou une passphrase, et l'utilisateur peut
répondre sans limite de temps. La même tentative faite après le démarrage de
l'interface poserait sa question dans un terminal que Textual contrôle, où
personne ne peut la lire ni y répondre.

Deux échecs, deux traitements. Pas de transport du tout (tunnel impossible)
est fatal : une interface n'aurait rien à afficher et rien à réessayer. Un
transport ouvert mais un daemon éteint reste non fatal, conformément au
paragraphe ci-dessus.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence

from dsup import __version__, tunnel
from dsup.app import DsupApp
from dsup.docker import DaemonUnreachable, DockerError, DockerGateway, connect
from dsup.tunnel import Target, TunnelError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dsup",
        description="Superviseur de conteneurs Docker dans le terminal.",
        epilog=(
            "en local : dsup · en remote : dsup user@hôte (ou un alias "
            "~/.ssh/config) — rien n'est installé ni exécuté sur le serveur"
        ),
    )
    parser.add_argument(
        "target",
        nargs="?",
        default="",
        metavar="CIBLE",
        help=(
            "hôte distant : alias ~/.ssh/config, user@hôte, ou ssh://… "
            "Par défaut : DOCKER_HOST, sinon le socket local"
        ),
    )
    parser.add_argument(
        "--refresh",
        type=float,
        default=2.0,
        metavar="SECONDES",
        help="intervalle de rafraîchissement de la liste (défaut : 2)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="inclure les conteneurs arrêtés",
    )
    parser.add_argument(
        "--filter",
        default="",
        metavar="MOTIF",
        help="filtre initial sur le nom ou l'image",
    )
    parser.add_argument(
        "--remote-socket",
        default=tunnel.DEFAULT_REMOTE_SOCKET,
        metavar="CHEMIN",
        help=(
            "socket Docker sur l'hôte distant "
            f"(défaut : {tunnel.DEFAULT_REMOTE_SOCKET} ; "
            "Docker rootless : /run/user/<uid>/docker.sock)"
        ),
    )
    parser.add_argument(
        "--disconnect",
        action="store_true",
        help="ferme le tunnel de la cible et quitte",
    )
    parser.add_argument(
        "--version", action="version", version=f"dsup {__version__}"
    )
    return parser


def _resolve_target(raw: str) -> Target | None:
    """Cible explicite, sinon celle d'un DOCKER_HOST en ssh://, sinon aucune."""
    if raw:
        return tunnel.normalize_target(raw)
    return tunnel.env_target()


def _fail(message: str, hint: str = "") -> int:
    print(f"dsup : {message}", file=sys.stderr)
    if hint:
        print(f"       {hint}", file=sys.stderr)
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.refresh <= 0:
        parser.error("--refresh attend un nombre de secondes strictement positif")

    try:
        target = _resolve_target(args.target)
    except TunnelError as err:
        return _fail(str(err), err.hint)

    if args.disconnect:
        if target is None:
            return _fail("--disconnect attend une cible", "exemple : dsup --disconnect root@hôte")
        if tunnel.close(target):
            print(f"tunnel fermé : {target.destination}")
        else:
            print(f"aucun tunnel ouvert pour {target.destination}")
        return 0

    gateway_factory = connect
    if target is not None:
        # Ouvre — ou retrouve — le tunnel pendant que le terminal est normal.
        try:
            tunnel.ensure(
                target, remote_socket=args.remote_socket, interactive=True
            )
        except TunnelError as err:
            return _fail(str(err), err.hint)
        os.environ["DOCKER_HOST"] = tunnel.docker_host(target)
        gateway_factory = _reconnecting_factory(target, args.remote_socket)

    # `connect()` directement, pas la fabrique : le tunnel vient d'être
    # ouvert ci-dessus, la revérifier ne ferait qu'un aller-retour de plus.
    # La fabrique n'existe que pour les reconnexions ultérieures.
    gateway: DockerGateway | None = None
    try:
        gateway = connect()
    except DockerError as err:
        # Démarrage en état dégradé, la boucle de collecte réessaiera. La
        # cause est écrite ici parce que l'interface va prendre le terminal :
        # ce message serait sinon perdu.
        print(f"dsup : {err}", file=sys.stderr)
        if err.hint:
            print(f"       {err.hint}", file=sys.stderr)

    app = DsupApp(
        gateway_factory,
        refresh=args.refresh,
        show_all=args.all,
        initial_filter=args.filter,
        gateway=gateway,
    )
    try:
        app.run()
    except DockerError as err:
        return _fail(str(err), err.hint or "")
    return app.return_code or 0


def _reconnecting_factory(
    target: Target, remote_socket: str
) -> Callable[[], DockerGateway]:
    """Fabrique de passerelle qui remonte le tunnel avant de se connecter.

    Toute la reprise sur incident tient ici : la boucle de collecte rappelle
    cette fabrique après une coupure, et le tunnel est rétabli au passage.
    `interactive=False` est la garantie qui protège l'interface — la relance
    réussit silencieusement si aucune saisie n'est requise (clé SSH, ou
    connexion maîtresse encore valide) et échoue proprement sinon, plutôt que
    de réclamer un mot de passe par-dessus l'affichage.
    """

    def factory() -> DockerGateway:
        try:
            tunnel.ensure(target, remote_socket=remote_socket, interactive=False)
        except TunnelError as err:
            raise DaemonUnreachable(str(err), hint=err.hint) from err
        return connect()

    return factory


if __name__ == "__main__":
    sys.exit(main())
