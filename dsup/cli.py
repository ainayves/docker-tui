"""Point d'entrée : parsing des arguments et lancement de l'application.

Le daemon absent au lancement n'est pas une erreur fatale : l'application
démarre et affiche l'état dégradé avec nouvel essai automatique. Les erreurs
qui empêchent tout de même le démarrage sortent en message clair, sans
traceback.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from dsup import __version__
from dsup.app import DsupApp
from dsup.docker import DockerError, connect


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dsup",
        description="Superviseur de conteneurs Docker dans le terminal.",
        epilog=(
            "en local : dsup · en remote : DOCKER_HOST=ssh://user@hôte dsup "
            "(rien à installer côté distant)"
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
        "--version", action="version", version=f"dsup {__version__}"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.refresh <= 0:
        parser.error("--refresh attend un nombre de secondes strictement positif")
    app = DsupApp(
        connect,
        refresh=args.refresh,
        show_all=args.all,
        initial_filter=args.filter,
    )
    try:
        app.run()
    except DockerError as err:
        print(f"dsup : {err}", file=sys.stderr)
        if err.hint:
            print(f"       {err.hint}", file=sys.stderr)
        return 1
    return app.return_code or 0


if __name__ == "__main__":
    sys.exit(main())
