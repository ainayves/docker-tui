"""Doubles de test partagés : gateway Docker en mémoire, fabrique de conteneurs."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from dsup.docker import (
    ContainerGone,
    ContainerInfo,
    ContainerState,
    DockerError,
    LogStream,
)
from dsup.stats import StatsReading

_BASE = ContainerInfo(
    id="3f9c21ab04e7" + "0" * 52,
    name="api",
    image="ghcr.io/acme/api:1.4",
    state=ContainerState.RUNNING,
    status_raw="Up 3 days",
    exit_code=None,
    created_at=None,
    started_at=None,
    ports=("8080",),
    health=None,
    restart_policy=None,
    restart_count=0,
)


def make_container(**overrides: Any) -> ContainerInfo:
    """Fabrique un ContainerInfo réaliste, surchargeable champ à champ."""
    return replace(_BASE, **overrides)


class FakeGateway:
    """Implémentation en mémoire de DockerGateway ; aucun daemon requis.

    `error` non nul fait échouer tous les appels réseau, pour simuler un
    daemon absent ou une coupure en cours de route.
    """

    def __init__(
        self,
        containers: list[ContainerInfo] | None = None,
        error: DockerError | None = None,
    ) -> None:
        self.containers = list(containers or [])
        self.error = error
        self.actions: list[tuple[str, str]] = []
        self.readings: dict[str, StatsReading] = {}
        self.log_lines: dict[str, list[bytes]] = {}
        self.closed = False

    def ping(self) -> None:
        self._maybe_fail()

    def list_containers(self, include_stopped: bool) -> list[ContainerInfo]:
        self._maybe_fail()
        if include_stopped:
            return list(self.containers)
        return [c for c in self.containers if c.state.is_up]

    def inspect(self, container_id: str) -> ContainerInfo:
        self._maybe_fail()
        return self._find(container_id)

    def sample_stats(self, container_id: str) -> StatsReading:
        self._maybe_fail()
        self._find(container_id)
        return self.readings.get(
            container_id, StatsReading(container_id, None, None, None, None)
        )

    def stream_logs(
        self, container_id: str, tail: int, timestamps: bool = True
    ) -> LogStream:
        self._maybe_fail()
        return LogStream(iter(list(self.log_lines.get(container_id, []))))

    def start(self, container_id: str) -> None:
        self._transition("start", container_id, ContainerState.RUNNING)

    def stop(self, container_id: str) -> None:
        self._transition("stop", container_id, ContainerState.EXITED)

    def restart(self, container_id: str) -> None:
        self._transition("restart", container_id, ContainerState.RUNNING)

    def kill(self, container_id: str) -> None:
        self._transition("kill", container_id, ContainerState.EXITED)

    def remove(self, container_id: str) -> None:
        self._maybe_fail()
        found = self._find(container_id)
        self.actions.append(("rm", container_id))
        self.containers.remove(found)

    def context_label(self) -> str:
        return "local"

    def close(self) -> None:
        self.closed = True

    def _maybe_fail(self) -> None:
        if self.error is not None:
            raise self.error

    def _find(self, container_id: str) -> ContainerInfo:
        for container in self.containers:
            if container.id == container_id:
                return container
        raise ContainerGone(f"conteneur {container_id[:12]} introuvable")

    def _transition(
        self, action: str, container_id: str, state: ContainerState
    ) -> None:
        self._maybe_fail()
        found = self._find(container_id)
        self.actions.append((action, container_id))
        exit_code = 0 if state is ContainerState.EXITED else None
        self.containers[self.containers.index(found)] = replace(
            found, state=state, exit_code=exit_code
        )
