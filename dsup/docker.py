"""Couche d'accès Docker.

Seul module du package qui importe docker-py. Il expose des dataclasses
immuables, des exceptions métier au message actionnable, et un protocole
`DockerGateway` que les tests remplacent par un faux en mémoire.

Tous les appels du protocole sont bloquants et synchrones : c'est l'appelant
(l'application Textual) qui les exécute hors de la boucle d'événements.
"""

from __future__ import annotations

import enum
import os
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

import docker  # type: ignore[import-untyped]
import docker.errors  # type: ignore[import-untyped]

from dsup.stats import StatsReading, parse_stats_dict

_CONTROL_TIMEOUT_S = 8
_STOP_TIMEOUT_S = 10


class ContainerState(enum.Enum):
    """État affichable d'un conteneur (symbole + mot + couleur dans l'UI)."""

    RUNNING = "running"
    RESTARTING = "restarting"
    PAUSED = "paused"
    CREATED = "created"
    EXITED = "exited"
    ERROR = "error"

    @property
    def is_up(self) -> bool:
        return self in (ContainerState.RUNNING, ContainerState.RESTARTING)


_SORT_ORDER: dict[ContainerState, int] = {
    ContainerState.RUNNING: 0,
    ContainerState.RESTARTING: 0,
    ContainerState.PAUSED: 1,
    ContainerState.CREATED: 1,
    ContainerState.EXITED: 1,
    ContainerState.ERROR: 2,
}


@dataclass(frozen=True, slots=True)
class ContainerInfo:
    """Instantané d'un conteneur, découplé des objets docker-py.

    L'endpoint liste ne fournit ni StartedAt ni RestartPolicy : construits
    depuis un listing, ces champs restent None/0 et sont complétés par
    `inspect()` en tâche de fond.
    """

    id: str
    name: str
    image: str
    state: ContainerState
    status_raw: str
    exit_code: int | None
    created_at: datetime | None
    started_at: datetime | None
    ports: tuple[str, ...]
    health: str | None
    restart_policy: str | None
    restart_count: int


class DockerError(Exception):
    """Erreur métier ; `hint` propose l'action corrective, en clair."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


class DaemonUnreachable(DockerError):
    """Daemon injoignable : socket absent, connexion refusée, tunnel tombé."""


class PermissionDenied(DockerError):
    """Accès refusé au socket Docker."""


class ApiVersionMismatch(DockerError):
    """Version d'API incompatible entre le client et le daemon."""


class ContainerGone(DockerError):
    """Le conteneur a disparu entre deux cycles ; à ignorer silencieusement."""


class OperationFailed(DockerError):
    """Le daemon a refusé une action (start/stop/kill/rm...)."""


class LogStream:
    """Flux de logs suivi ; `close()` est appelable depuis un autre thread.

    Enveloppe l'itérateur de docker-py (CancellableStream) pour traduire les
    erreurs et pouvoir débloquer un thread lecteur en fermant le socket.
    """

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def __iter__(self) -> Iterator[bytes]:
        try:
            for chunk in self._raw:
                if isinstance(chunk, bytes):
                    yield chunk
        except Exception as exc:
            raise translate_error(exc) from exc

    def close(self) -> None:
        close = getattr(self._raw, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


class DockerGateway(Protocol):
    """Contrat de la couche Docker ; remplaçable par un faux dans les tests."""

    def ping(self) -> None: ...

    def list_containers(self, include_stopped: bool) -> list[ContainerInfo]: ...

    def inspect(self, container_id: str) -> ContainerInfo: ...

    def sample_stats(self, container_id: str) -> StatsReading: ...

    def stream_logs(
        self, container_id: str, tail: int, timestamps: bool = True
    ) -> LogStream: ...

    def start(self, container_id: str) -> None: ...

    def stop(self, container_id: str) -> None: ...

    def restart(self, container_id: str) -> None: ...

    def kill(self, container_id: str) -> None: ...

    def remove(self, container_id: str) -> None: ...

    def context_label(self) -> str: ...

    def close(self) -> None: ...


def filter_containers(items: Iterable[ContainerInfo], needle: str) -> list[ContainerInfo]:
    """Filtre incrémental : sous-chaîne insensible à la casse, nom ou image."""
    needle = needle.strip().lower()
    if not needle:
        return list(items)
    return [c for c in items if needle in c.name.lower() or needle in c.image.lower()]


def sort_containers(items: Iterable[ContainerInfo]) -> list[ContainerInfo]:
    """Tri de la maquette : up d'abord, puis arrêtés, puis erreurs ; nom ensuite."""
    return sorted(items, key=lambda c: (_SORT_ORDER.get(c.state, 1), c.name.lower()))


class DockerPyGateway:
    """Implémentation du protocole au-dessus du client bas niveau docker-py.

    `stream_api` est un second client sans timeout de lecture, réservé au
    suivi des logs : un flux silencieux ne doit pas être coupé par le
    timeout court des appels de contrôle.
    """

    def __init__(self, api: Any, context: str, stream_api: Any | None = None) -> None:
        self._api = api
        self._stream_api = stream_api if stream_api is not None else api
        self._context = context

    def ping(self) -> None:
        try:
            self._api.ping()
        except Exception as exc:
            raise translate_error(exc) from exc

    def list_containers(self, include_stopped: bool) -> list[ContainerInfo]:
        try:
            entries = self._api.containers(all=include_stopped)
        except Exception as exc:
            raise translate_error(exc) from exc
        return [info_from_listing(e) for e in entries if isinstance(e, Mapping)]

    def inspect(self, container_id: str) -> ContainerInfo:
        try:
            attrs = self._api.inspect_container(container_id)
        except Exception as exc:
            raise translate_error(exc) from exc
        return info_from_inspect(attrs)

    def sample_stats(self, container_id: str) -> StatsReading:
        try:
            try:
                raw = self._api.stats(container_id, stream=False, one_shot=True)
            except docker.errors.InvalidVersion:
                # Daemon < API 1.41 : le mode one_shot n'existe pas encore.
                raw = self._api.stats(container_id, stream=False)
        except Exception as exc:
            raise translate_error(exc) from exc
        return parse_stats_dict(container_id, raw if isinstance(raw, Mapping) else {})

    def stream_logs(
        self, container_id: str, tail: int, timestamps: bool = True
    ) -> LogStream:
        try:
            raw = self._stream_api.logs(
                container_id,
                stream=True,
                follow=True,
                tail=tail,
                timestamps=timestamps,
            )
        except Exception as exc:
            raise translate_error(exc) from exc
        return LogStream(raw)

    def start(self, container_id: str) -> None:
        self._call("start", self._api.start, container_id)

    def stop(self, container_id: str) -> None:
        self._call("stop", self._api.stop, container_id, timeout=_STOP_TIMEOUT_S)

    def restart(self, container_id: str) -> None:
        self._call("restart", self._api.restart, container_id, timeout=_STOP_TIMEOUT_S)

    def kill(self, container_id: str) -> None:
        self._call("kill", self._api.kill, container_id)

    def remove(self, container_id: str) -> None:
        self._call("rm", self._api.remove_container, container_id)

    def context_label(self) -> str:
        return self._context

    def close(self) -> None:
        for api in {id(self._api): self._api, id(self._stream_api): self._stream_api}.values():
            try:
                api.close()
            except Exception:
                pass

    def _call(self, action: str, fn: Any, *args: Any, **kwargs: Any) -> None:
        try:
            fn(*args, **kwargs)
        except Exception as exc:
            err = translate_error(exc)
            if isinstance(err, OperationFailed):
                err = OperationFailed(f"{action} impossible : {err}", hint=err.hint)
            raise err from exc


def connect() -> DockerGateway:
    """Ouvre la connexion au daemon en honorant DOCKER_HOST (unix, tcp, ssh://).

    `use_ssh_client=True` délègue ssh:// au binaire ssh du système : la
    config ~/.ssh/config s'applique et rien n'est requis côté distant.
    `version="auto"` négocie la version d'API dès la connexion, donc les
    daemons anciens restent utilisables.
    """
    context = context_label_from_env()
    try:
        control = docker.from_env(
            version="auto", timeout=_CONTROL_TIMEOUT_S, use_ssh_client=True
        )
        stream = docker.from_env(version="auto", timeout=None, use_ssh_client=True)
    except Exception as exc:
        raise translate_error(exc) from exc
    return DockerPyGateway(control.api, context, stream_api=stream.api)


def context_label_from_env() -> str:
    """Libellé du contexte pour la barre du haut : "local", "ssh:hôte", "tcp:hôte"."""
    host = os.environ.get("DOCKER_HOST", "").strip()
    if not host:
        return "local"
    scheme, sep, rest = host.partition("://")
    if not sep:
        return host
    if scheme.startswith("unix") or scheme == "npipe":
        return "local"
    authority = rest.split("/", 1)[0]
    hostname = authority.rsplit("@", 1)[-1].rsplit(":", 1)[0] or authority
    label = "tcp" if scheme in ("tcp", "http", "https") else scheme
    return f"{label}:{hostname}"


_EXIT_CODE_RE = re.compile(r"Exited \((-?\d+)\)")
_HEALTH_RE = re.compile(r"\((healthy|unhealthy|health: starting)\)")
_TIME_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})$"
)

_TRANSPORT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("connection refused", "connexion refusée"),
    ("no such file or directory", "socket introuvable"),
    ("timed out", "délai dépassé"),
    ("timeout", "délai dépassé"),
    ("broken pipe", "connexion interrompue"),
    ("connection aborted", "connexion interrompue"),
    ("connection reset", "connexion interrompue"),
    ("name or service not known", "hôte inconnu"),
    ("nodename nor servname", "hôte inconnu"),
)


def translate_error(exc: BaseException) -> DockerError:
    """Convertit une exception docker-py/requests en erreur métier actionnable."""
    if isinstance(exc, DockerError):
        return exc
    message = str(exc) or exc.__class__.__name__
    lowered = message.lower()
    if isinstance(exc, docker.errors.NotFound):
        return ContainerGone("conteneur introuvable (déjà supprimé ?)")
    if "permission denied" in lowered:
        return PermissionDenied(
            f"permission refusée sur {_endpoint_label()}",
            hint=(
                "ajoute ton utilisateur au groupe docker : "
                "sudo usermod -aG docker $USER, puis ouvre une nouvelle session"
            ),
        )
    if isinstance(exc, docker.errors.InvalidVersion) or (
        "version" in lowered and ("too new" in lowered or "too old" in lowered)
    ):
        return ApiVersionMismatch(
            "version d'API incompatible entre dsup et le daemon",
            hint="compare les versions avec `docker version`, puis mets à jour docker-py ou le daemon",
        )
    if isinstance(exc, docker.errors.APIError):
        explanation = getattr(exc, "explanation", None)
        detail = explanation if isinstance(explanation, str) and explanation else message
        return OperationFailed(detail)
    # Tout le reste relève du transport : socket absent, connexion refusée,
    # tunnel SSH tombé, délai dépassé.
    reason = _describe_transport(lowered, fallback=message)
    return DaemonUnreachable(
        f"{_endpoint_label()} : {reason}", hint=_unreachable_hint()
    )


def _describe_transport(lowered: str, fallback: str) -> str:
    for needle, label in _TRANSPORT_PATTERNS:
        if needle in lowered:
            return label
    return fallback[:120]


def _endpoint_label() -> str:
    host = os.environ.get("DOCKER_HOST", "").strip()
    return host or "/var/run/docker.sock"


def _unreachable_hint() -> str:
    host = os.environ.get("DOCKER_HOST", "").strip()
    if host.startswith("ssh://"):
        target = host.removeprefix("ssh://")
        return f"vérifie l'accès : ssh {target} docker info"
    if host:
        return f"vérifie que le daemon écoute sur {host} : docker -H {host} info"
    return "vérifie que le daemon tourne : systemctl status docker, ou ouvre Docker Desktop"


def info_from_listing(entry: Mapping[str, Any]) -> ContainerInfo:
    """Construit un ContainerInfo depuis une entrée de /containers/json."""
    container_id = str(entry.get("Id") or "")
    names = entry.get("Names")
    name = (
        str(names[0]).lstrip("/")
        if isinstance(names, list) and names
        else container_id[:12]
    )
    status = str(entry.get("Status") or "")
    exit_code = _exit_code_from_status(status)
    created_raw = entry.get("Created")
    created = (
        datetime.fromtimestamp(created_raw, tz=timezone.utc)
        if isinstance(created_raw, (int, float))
        else None
    )
    ports_raw = entry.get("Ports")
    return ContainerInfo(
        id=container_id,
        name=name,
        image=str(entry.get("Image") or "?"),
        state=_state_of(str(entry.get("State") or ""), exit_code),
        status_raw=status,
        exit_code=exit_code,
        created_at=created,
        started_at=None,
        ports=_ports_from_listing(ports_raw if isinstance(ports_raw, list) else []),
        health=_health_from_status(status),
        restart_policy=None,
        restart_count=0,
    )


def info_from_inspect(attrs: Mapping[str, Any]) -> ContainerInfo:
    """Construit un ContainerInfo complet depuis /containers/{id}/json."""
    state_section = attrs.get("State")
    state: Mapping[str, Any] = state_section if isinstance(state_section, Mapping) else {}
    raw_state = str(state.get("Status") or "")
    exit_code_raw = state.get("ExitCode")
    exit_code = (
        exit_code_raw
        if isinstance(exit_code_raw, int)
        and raw_state in ("exited", "dead", "removing", "restarting")
        else None
    )
    health_section = state.get("Health")
    health = (
        str(health_section.get("Status"))
        if isinstance(health_section, Mapping) and health_section.get("Status")
        else None
    )
    config = attrs.get("Config")
    image = (
        str(config.get("Image"))
        if isinstance(config, Mapping) and config.get("Image")
        else str(attrs.get("Image") or "?")
    )
    host_config = attrs.get("HostConfig")
    policy_section = (
        host_config.get("RestartPolicy") if isinstance(host_config, Mapping) else None
    )
    restart_policy = (
        str(policy_section.get("Name"))
        if isinstance(policy_section, Mapping) and policy_section.get("Name")
        else None
    )
    restart_count_raw = attrs.get("RestartCount")
    network = attrs.get("NetworkSettings")
    ports_section = network.get("Ports") if isinstance(network, Mapping) else None
    return ContainerInfo(
        id=str(attrs.get("Id") or ""),
        name=str(attrs.get("Name") or "").lstrip("/"),
        image=image,
        state=_state_of(raw_state, exit_code),
        status_raw=raw_state,
        exit_code=exit_code,
        created_at=parse_docker_time(attrs.get("Created")),
        started_at=parse_docker_time(state.get("StartedAt")),
        ports=_ports_from_inspect(ports_section if isinstance(ports_section, Mapping) else {}),
        health=health,
        restart_policy=restart_policy,
        restart_count=restart_count_raw if isinstance(restart_count_raw, int) else 0,
    )


def parse_docker_time(value: Any) -> datetime | None:
    """Décode un horodatage Docker (RFC 3339, fraction nanoseconde possible).

    "0001-01-01T00:00:00Z" est la valeur zéro de Go : jamais démarré → None.
    """
    if not isinstance(value, str) or value.startswith("0001-01-01"):
        return None
    match = _TIME_RE.match(value)
    if match is None:
        return None
    base, frac, tz = match.groups()
    micro = (frac or "0")[:6].ljust(6, "0")
    offset = "+00:00" if tz == "Z" else tz
    try:
        return datetime.fromisoformat(f"{base}.{micro}{offset}")
    except ValueError:
        return None


def _state_of(raw: str, exit_code: int | None) -> ContainerState:
    if raw == "running":
        return ContainerState.RUNNING
    if raw == "restarting":
        return ContainerState.RESTARTING
    if raw == "paused":
        return ContainerState.PAUSED
    if raw == "created":
        return ContainerState.CREATED
    if raw == "dead":
        return ContainerState.ERROR
    if raw in ("exited", "removing") and exit_code not in (None, 0):
        return ContainerState.ERROR
    return ContainerState.EXITED


def _exit_code_from_status(status: str) -> int | None:
    match = _EXIT_CODE_RE.search(status)
    return int(match.group(1)) if match else None


def _health_from_status(status: str) -> str | None:
    match = _HEALTH_RE.search(status)
    if match is None:
        return None
    label = match.group(1)
    return "starting" if label == "health: starting" else label


def _ports_from_listing(entries: list[Any]) -> tuple[str, ...]:
    published: set[int] = set()
    for entry in entries:
        if isinstance(entry, Mapping):
            public = entry.get("PublicPort")
            if isinstance(public, int):
                published.add(public)
    return tuple(str(p) for p in sorted(published))


def _ports_from_inspect(mapping: Mapping[str, Any]) -> tuple[str, ...]:
    published: set[int] = set()
    for bindings in mapping.values():
        if not isinstance(bindings, list):
            continue
        for binding in bindings:
            if isinstance(binding, Mapping):
                host_port = str(binding.get("HostPort") or "")
                if host_port.isdigit():
                    published.add(int(host_port))
    return tuple(str(p) for p in sorted(published))
