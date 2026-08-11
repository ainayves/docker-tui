"""Tests de la couche Docker : erreurs traduites, décodage, daemon absent."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import docker.errors  # type: ignore[import-untyped]
import pytest
from conftest import FakeGateway, make_container

import dsup.docker as layer
from dsup.docker import (
    ApiVersionMismatch,
    ContainerGone,
    ContainerState,
    DaemonUnreachable,
    DockerPyGateway,
    OperationFailed,
    PermissionDenied,
    context_label_from_env,
    info_from_inspect,
    info_from_listing,
    parse_docker_time,
    translate_error,
)

# ---------------------------------------------------------------- erreurs


class TestTranslateError:
    def test_not_found_devient_container_gone(self) -> None:
        assert isinstance(translate_error(docker.errors.NotFound("no")), ContainerGone)

    def test_permission_refusee_avec_conseil(self) -> None:
        err = translate_error(
            docker.errors.DockerException(
                "Error while fetching server API version: "
                "('Connection aborted.', PermissionError(13, 'Permission denied'))"
            )
        )
        assert isinstance(err, PermissionDenied)
        assert err.hint is not None and "groupe docker" in err.hint

    def test_version_incompatible(self) -> None:
        assert isinstance(
            translate_error(docker.errors.InvalidVersion("nope")), ApiVersionMismatch
        )
        assert isinstance(
            translate_error(
                docker.errors.DockerException(
                    "client version 1.45 is too new. "
                    "Maximum supported API version is 1.41"
                )
            ),
            ApiVersionMismatch,
        )

    def test_refus_du_daemon_devient_operation_failed(self) -> None:
        err = translate_error(
            docker.errors.APIError(
                "409 Client Error",
                explanation="cannot remove a running container",
            )
        )
        assert isinstance(err, OperationFailed)
        assert "running container" in str(err)

    def test_transport_devient_daemon_unreachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DOCKER_HOST", raising=False)
        err = translate_error(ConnectionError("[Errno 61] Connection refused"))
        assert isinstance(err, DaemonUnreachable)
        assert "connexion refusée" in str(err)
        assert "/var/run/docker.sock" in str(err)
        assert err.hint is not None

    def test_conseil_ssh_quand_docker_host_ssh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DOCKER_HOST", "ssh://admin@prod-eu-1")
        err = translate_error(ConnectionError("Connection reset by peer"))
        assert isinstance(err, DaemonUnreachable)
        assert err.hint is not None and "ssh admin@prod-eu-1" in err.hint

    def test_erreur_metier_passe_inchangee(self) -> None:
        original = ContainerGone("déjà parti")
        assert translate_error(original) is original


def test_connect_daemon_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**kwargs: Any) -> Any:
        raise docker.errors.DockerException(
            "Error while fetching server API version: Connection refused"
        )

    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr("dsup.docker.docker.from_env", boom)
    with pytest.raises(DaemonUnreachable) as exc_info:
        layer.connect()
    assert "connexion refusée" in str(exc_info.value)
    assert exc_info.value.hint is not None


# ---------------------------------------------------------------- décodage

_LISTING_UP: dict[str, Any] = {
    "Id": "3f9c21ab04e7" + "0" * 52,
    "Names": ["/api"],
    "Image": "ghcr.io/acme/api:1.4",
    "State": "running",
    "Status": "Up 3 days (healthy)",
    "Created": 1_722_758_400,
    "Ports": [
        {"IP": "0.0.0.0", "PrivatePort": 8080, "PublicPort": 8080, "Type": "tcp"},
        {"PrivatePort": 9000, "Type": "tcp"},
        {"IP": "::", "PrivatePort": 8080, "PublicPort": 8080, "Type": "tcp"},
    ],
}


class TestInfoFromListing:
    def test_conteneur_up(self) -> None:
        info = info_from_listing(_LISTING_UP)
        assert info.name == "api"
        assert info.state is ContainerState.RUNNING
        assert info.health == "healthy"
        assert info.ports == ("8080",)
        assert info.exit_code is None
        assert info.created_at == datetime.fromtimestamp(
            1_722_758_400, tz=timezone.utc
        )

    def test_exited_code_non_nul_est_une_erreur(self) -> None:
        entry = dict(
            _LISTING_UP, State="exited", Status="Exited (137) 3 hours ago", Ports=[]
        )
        info = info_from_listing(entry)
        assert info.state is ContainerState.ERROR
        assert info.exit_code == 137
        assert info.ports == ()

    def test_exited_code_zero_reste_exited(self) -> None:
        entry = dict(_LISTING_UP, State="exited", Status="Exited (0) 2 hours ago")
        info = info_from_listing(entry)
        assert info.state is ContainerState.EXITED
        assert info.exit_code == 0

    def test_restarting(self) -> None:
        entry = dict(
            _LISTING_UP, State="restarting", Status="Restarting (137) 8 seconds ago"
        )
        info = info_from_listing(entry)
        assert info.state is ContainerState.RESTARTING

    def test_entree_minimale_sans_planter(self) -> None:
        info = info_from_listing({"Id": "abcdef123456"})
        assert info.name == "abcdef123456"
        assert info.state is ContainerState.EXITED
        assert info.ports == ()


_INSPECT: dict[str, Any] = {
    "Id": "3f9c21ab04e7" + "0" * 52,
    "Name": "/api",
    "Created": "2024-08-04T08:12:33.123456789Z",
    "State": {
        "Status": "running",
        "StartedAt": "2024-08-04T08:12:34.5Z",
        "ExitCode": 0,
        "Health": {"Status": "healthy"},
    },
    "Config": {"Image": "ghcr.io/acme/api:1.4"},
    "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
    "RestartCount": 3,
    "NetworkSettings": {
        "Ports": {
            "8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}],
            "9000/tcp": None,
        }
    },
}


class TestInfoFromInspect:
    def test_conteneur_complet(self) -> None:
        info = info_from_inspect(_INSPECT)
        assert info.name == "api"
        assert info.image == "ghcr.io/acme/api:1.4"
        assert info.state is ContainerState.RUNNING
        assert info.health == "healthy"
        assert info.restart_policy == "unless-stopped"
        assert info.restart_count == 3
        assert info.ports == ("8080",)
        assert info.exit_code is None
        assert info.started_at == datetime(
            2024, 8, 4, 8, 12, 34, 500000, tzinfo=timezone.utc
        )
        assert info.created_at is not None
        assert info.created_at.microsecond == 123456

    def test_restarting_garde_le_dernier_code(self) -> None:
        attrs = dict(_INSPECT, State={"Status": "restarting", "ExitCode": 137})
        info = info_from_inspect(attrs)
        assert info.state is ContainerState.RESTARTING
        assert info.exit_code == 137


class TestParseDockerTime:
    def test_fraction_nanoseconde(self) -> None:
        parsed = parse_docker_time("2024-08-04T08:12:33.123456789Z")
        assert parsed == datetime(2024, 8, 4, 8, 12, 33, 123456, tzinfo=timezone.utc)

    def test_jamais_demarre(self) -> None:
        assert parse_docker_time("0001-01-01T00:00:00Z") is None

    def test_valeurs_invalides(self) -> None:
        assert parse_docker_time(None) is None
        assert parse_docker_time("pas une date") is None


class TestContextLabel:
    def test_sans_docker_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DOCKER_HOST", raising=False)
        assert context_label_from_env() == "local"

    def test_ssh(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DOCKER_HOST", "ssh://admin@prod-eu-1")
        assert context_label_from_env() == "ssh:prod-eu-1"

    def test_tcp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DOCKER_HOST", "tcp://10.0.0.4:2376")
        assert context_label_from_env() == "tcp:10.0.0.4"

    def test_socket_unix_reste_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
        assert context_label_from_env() == "local"


# ---------------------------------------------------------------- gateway


class _FakeApi:
    """Client bas niveau simulé, piloté par les tests."""

    def __init__(
        self,
        listing: list[dict[str, Any]] | None = None,
        inspect_attrs: dict[str, dict[str, Any]] | None = None,
        stats_raw: dict[str, Any] | None = None,
        fail_with: Exception | None = None,
    ) -> None:
        self._listing = listing or []
        self._inspect = inspect_attrs or {}
        self._stats = stats_raw or {}
        self._fail = fail_with
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _guard(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))
        if self._fail is not None:
            raise self._fail

    def ping(self) -> bool:
        self._guard("ping")
        return True

    def containers(self, all: bool = False) -> list[dict[str, Any]]:
        self._guard("containers", all=all)
        return self._listing

    def inspect_container(self, container_id: str) -> dict[str, Any]:
        self._guard("inspect", container_id)
        if container_id not in self._inspect:
            raise docker.errors.NotFound("no such container")
        return self._inspect[container_id]

    def stats(self, container_id: str, **kwargs: Any) -> dict[str, Any]:
        self._guard("stats", container_id, **kwargs)
        return self._stats

    def logs(self, container_id: str, **kwargs: Any) -> Any:
        self._guard("logs", container_id, **kwargs)
        return iter([b"10:41:52 GET /health 200 1ms\n", b"10:41:53 ok\n"])

    def stop(self, container_id: str, timeout: int | None = None) -> None:
        self._guard("stop", container_id, timeout=timeout)

    def close(self) -> None:
        self._guard("close")


class TestDockerPyGateway:
    def test_liste_decodee(self) -> None:
        gateway = DockerPyGateway(_FakeApi(listing=[_LISTING_UP]), "local")
        containers = gateway.list_containers(include_stopped=True)
        assert [c.name for c in containers] == ["api"]

    def test_panne_transport_traduite(self) -> None:
        api = _FakeApi(fail_with=ConnectionError("Connection refused"))
        gateway = DockerPyGateway(api, "local")
        with pytest.raises(DaemonUnreachable):
            gateway.list_containers(include_stopped=True)

    def test_inspect_disparu_devient_container_gone(self) -> None:
        gateway = DockerPyGateway(_FakeApi(), "local")
        with pytest.raises(ContainerGone):
            gateway.inspect("introuvable")

    def test_action_refusee_nomme_l_action(self) -> None:
        api = _FakeApi(
            fail_with=docker.errors.APIError(
                "409", explanation="container is restarting"
            )
        )
        gateway = DockerPyGateway(api, "local")
        with pytest.raises(OperationFailed) as exc_info:
            gateway.stop("abc")
        assert "stop impossible" in str(exc_info.value)

    def test_sample_stats_decode(self) -> None:
        raw = {
            "cpu_stats": {
                "cpu_usage": {"total_usage": 10},
                "system_cpu_usage": 100,
                "online_cpus": 2,
            }
        }
        gateway = DockerPyGateway(_FakeApi(stats_raw=raw), "local")
        reading = gateway.sample_stats("abc")
        assert reading.cur is not None and reading.cur.online_cpus == 2
        assert reading.pre is None

    def test_stream_logs_rend_les_chunks(self) -> None:
        api = _FakeApi()
        gateway = DockerPyGateway(_FakeApi(), "local", stream_api=api)
        chunks = list(gateway.stream_logs("abc", tail=200))
        assert len(chunks) == 2
        # Le flux passe par le client sans timeout, pas par celui de contrôle.
        assert api.calls and api.calls[0][0] == "logs"


# ------------------------------------------------- comportement sans daemon


class TestFakeGatewayDaemonAbsent:
    def test_ping_echoue_proprement(self) -> None:
        gateway = FakeGateway(error=DaemonUnreachable("docker non joignable"))
        with pytest.raises(DaemonUnreachable):
            gateway.ping()

    def test_liste_echoue_proprement(self) -> None:
        gateway = FakeGateway(
            containers=[make_container()],
            error=DaemonUnreachable("coupure en cours de route"),
        )
        with pytest.raises(DaemonUnreachable):
            gateway.list_containers(include_stopped=True)

    def test_actions_sur_le_faux(self) -> None:
        container = make_container()
        gateway = FakeGateway(containers=[container])
        gateway.stop(container.id)
        assert gateway.actions == [("stop", container.id)]
        assert gateway.list_containers(include_stopped=True)[0].state is (
            ContainerState.EXITED
        )
        gateway.remove(container.id)
        assert gateway.list_containers(include_stopped=True) == []
        with pytest.raises(ContainerGone):
            gateway.inspect(container.id)
