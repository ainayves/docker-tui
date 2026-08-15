"""Tests de la ligne de commande : parsing, tunnel, démarrage de l'application."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeGateway

from dsup import __version__
from dsup.cli import build_parser, main
from dsup.docker import DaemonUnreachable
from dsup.tunnel import TunnelError


@pytest.fixture(autouse=True)
def _environnement_isole(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isole DOCKER_HOST et le dossier des sockets.

    `main()` écrit DOCKER_HOST directement dans l'environnement : sans cette
    remise à zéro, une cible posée par un test resterait visible par les
    suivants et le résultat dépendrait de l'ordre d'exécution.
    """
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


class _AppFactice:
    """Remplace DsupApp : capture les arguments, ne démarre aucune interface."""

    def __init__(self, capture: dict[str, Any]) -> None:
        self.capture = capture

    def __call__(self, factory: Any, **kwargs: Any) -> _AppFactice:
        self.capture["factory"] = factory
        self.capture.update(kwargs)
        return self

    def run(self) -> None:
        return None

    return_code = 0


def _sans_interface(
    monkeypatch: pytest.MonkeyPatch, *, tunnel_ouvre: bool = True
) -> dict[str, Any]:
    """Neutralise l'interface et l'ouverture de tunnel.

    Aucun test ne doit sortir sur le réseau : une suite dont la durée explose
    signalerait un vrai `ssh`.
    """
    capture: dict[str, Any] = {}
    monkeypatch.setattr("dsup.cli.DsupApp", _AppFactice(capture))

    capture["tunnels"] = []

    def ensure(target: Any, **kwargs: Any) -> Path:
        capture["tunnels"].append((target, kwargs))
        if not tunnel_ouvre:
            raise TunnelError("authentification refusée", hint="ssh-copy-id root@h")
        return Path("/tmp/factice.sock")

    monkeypatch.setattr("dsup.cli.tunnel.ensure", ensure)
    return capture


# ------------------------------------------------------------------ parsing


def test_valeurs_par_defaut() -> None:
    args = build_parser().parse_args([])
    assert args.refresh == 2.0
    assert args.all is False
    assert args.filter == ""
    assert args.target == ""
    assert args.remote_socket == "/var/run/docker.sock"
    assert args.disconnect is False


def test_options_explicites() -> None:
    args = build_parser().parse_args(
        ["root@h", "--refresh", "5", "--all", "--filter", "api"]
    )
    assert args.target == "root@h"
    assert args.refresh == 5.0
    assert args.all is True
    assert args.filter == "api"


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["--version"])
    assert exc_info.value.code == 0
    assert f"dsup {__version__}" in capsys.readouterr().out


def test_refresh_invalide() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--refresh", "0"])
    assert exc_info.value.code == 2


def test_refresh_non_numerique() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--refresh", "abc"])
    assert exc_info.value.code == 2


# ------------------------------------------------------------------- tunnel


def test_cible_pointe_docker_host_sur_la_socket_locale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le tunnel transforme une cible SSH en transport unix local.

    C'est ce basculement qui fait que docker-py n'invoque plus jamais `ssh`,
    donc qu'aucun prompt ne peut surgir sous l'interface.
    """
    monkeypatch.setattr("dsup.cli.connect", lambda: FakeGateway())
    _sans_interface(monkeypatch)

    assert main(["root@10.0.0.1"]) == 0
    assert os.environ["DOCKER_HOST"].startswith("unix://")
    assert os.environ["DOCKER_HOST"].endswith(".sock")


def test_cible_prime_sur_l_environnement(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un DOCKER_HOST oublié dans un shell ne doit pas détourner la connexion."""
    monkeypatch.setenv("DOCKER_HOST", "tcp://oublie:2375")
    monkeypatch.setattr("dsup.cli.connect", lambda: FakeGateway())
    capture = _sans_interface(monkeypatch)

    assert main(["root@10.0.0.1"]) == 0
    cible, _ = capture["tunnels"][0]
    assert cible.host == "10.0.0.1"
    assert os.environ["DOCKER_HOST"].startswith("unix://")


def test_docker_host_ssh_declenche_un_tunnel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sans argument, un DOCKER_HOST en ssh:// passe aussi par le tunnel."""
    monkeypatch.setenv("DOCKER_HOST", "ssh://root@10.0.0.1")
    monkeypatch.setattr("dsup.cli.connect", lambda: FakeGateway())
    capture = _sans_interface(monkeypatch)

    assert main([]) == 0
    cible, _ = capture["tunnels"][0]
    assert cible.host == "10.0.0.1"


def test_socket_distante_transmise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("dsup.cli.connect", lambda: FakeGateway())
    capture = _sans_interface(monkeypatch)

    main(["root@h", "--remote-socket", "/run/user/1000/docker.sock"])

    _, kwargs = capture["tunnels"][0]
    assert kwargs["remote_socket"] == "/run/user/1000/docker.sock"


def test_premier_tunnel_est_interactif(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avant l'interface, ssh doit pouvoir demander le mot de passe."""
    monkeypatch.setattr("dsup.cli.connect", lambda: FakeGateway())
    capture = _sans_interface(monkeypatch)

    main(["root@h"])

    _, kwargs = capture["tunnels"][0]
    assert kwargs["interactive"] is True


def test_tunnel_impossible_echoue_vite(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Sans transport, une interface n'aurait rien à afficher ni à réessayer."""
    _sans_interface(monkeypatch, tunnel_ouvre=False)

    assert main(["root@h"]) == 1
    err = capsys.readouterr().err
    assert "authentification refusée" in err
    assert "ssh-copy-id root@h" in err


def test_cible_illisible(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["tcp://10.0.0.1:2375"]) == 1
    assert "non géré" in capsys.readouterr().err


def test_disconnect(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("dsup.cli.tunnel.close", lambda _: True)
    assert main(["root@h", "--disconnect"]) == 0
    assert "tunnel fermé" in capsys.readouterr().out


def test_disconnect_sans_tunnel(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("dsup.cli.tunnel.close", lambda _: False)
    assert main(["root@h", "--disconnect"]) == 0
    assert "aucun tunnel" in capsys.readouterr().out


def test_disconnect_sans_cible(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--disconnect"]) == 1
    assert "attend une cible" in capsys.readouterr().err


# -------------------------------------------------------------- application


def test_connexion_ouverte_avant_l_interface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La passerelle est ouverte par la CLI et transmise à l'application."""
    passerelle = FakeGateway()
    monkeypatch.setattr("dsup.cli.connect", lambda: passerelle)
    capture = _sans_interface(monkeypatch)

    assert main([]) == 0
    assert capture["gateway"] is passerelle


def test_daemon_absent_reste_non_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tunnel ouvert mais daemon éteint : on démarre en état dégradé.

    À distinguer d'un tunnel impossible, qui est fatal : ici le transport
    existe, donc la boucle de collecte a une chance d'aboutir en réessayant.
    """

    def echoue() -> FakeGateway:
        raise DaemonUnreachable("daemon injoignable")

    monkeypatch.setattr("dsup.cli.connect", echoue)
    capture = _sans_interface(monkeypatch)

    assert main(["root@h"]) == 0
    assert capture["gateway"] is None
    assert capture["factory"] is not None


def test_reconnexion_est_muette(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invariant décisif : la fabrique de reconnexion ne demande jamais rien.

    Elle est appelée par la boucle de collecte, pendant que l'interface tient
    le terminal. Un `interactive=True` ici ramènerait la tempête de prompts
    qui rendait la navigation inutilisable.
    """
    monkeypatch.setattr("dsup.cli.connect", lambda: FakeGateway())
    capture = _sans_interface(monkeypatch)
    main(["root@h"])

    capture["factory"]()  # simule une reconnexion déclenchée par _poll_loop

    _, kwargs = capture["tunnels"][-1]
    assert kwargs["interactive"] is False


def test_reconnexion_impossible_leve_une_erreur_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L'échec de tunnel doit parler le langage de la boucle de collecte."""
    monkeypatch.setattr("dsup.cli.connect", lambda: FakeGateway())
    capture = _sans_interface(monkeypatch)
    main(["root@h"])

    def refuse(target: Any, **kwargs: Any) -> Path:
        raise TunnelError("lien perdu", hint="vérifiez le réseau")

    monkeypatch.setattr("dsup.cli.tunnel.ensure", refuse)
    with pytest.raises(DaemonUnreachable) as exc:
        capture["factory"]()
    assert "lien perdu" in str(exc.value)
    assert exc.value.hint == "vérifiez le réseau"
