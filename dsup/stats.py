"""Collecte et calcul des métriques conteneur.

Module pur : aucune I/O, aucune dépendance à docker-py. Les dictionnaires
bruts de l'endpoint /containers/{id}/stats sont décodés ici en dataclasses,
et le CPU % est calculé en delta entre deux relevés.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class CpuSample:
    """Relevé CPU brut, suffisant pour un calcul de delta."""

    cpu_total: int
    system_total: int | None
    online_cpus: int


@dataclass(frozen=True, slots=True)
class ContainerStats:
    """Métriques prêtes à afficher ; None = indisponible à ce relevé."""

    container_id: str
    cpu_percent: float | None
    mem_used_bytes: int | None
    mem_limit_bytes: int | None


@dataclass(frozen=True, slots=True)
class StatsReading:
    """Un relevé one-shot de l'API stats, déjà décodé.

    `pre` est la section precpu_stats embarquée par Docker dans le même
    relevé ; elle est absente (None) au premier relevé ou en mode one-shot.
    """

    container_id: str
    cur: CpuSample | None
    pre: CpuSample | None
    mem_used_bytes: int | None
    mem_limit_bytes: int | None


def compute_cpu_percent(prev: CpuSample | None, cur: CpuSample | None) -> float | None:
    """CPU % = cpu_delta / system_delta * nb_cpus * 100.

    Renvoie None quand le delta est incalculable : premier relevé, compteur
    système absent (conteneur arrêté, cgroup partiel), delta système nul ou
    compteurs repartis à zéro après un redémarrage.
    """
    if prev is None or cur is None:
        return None
    if prev.system_total is None or cur.system_total is None:
        return None
    cpu_delta = cur.cpu_total - prev.cpu_total
    system_delta = cur.system_total - prev.system_total
    if system_delta <= 0 or cpu_delta < 0:
        return None
    return cpu_delta / system_delta * cur.online_cpus * 100.0


def parse_stats_dict(container_id: str, raw: Mapping[str, Any]) -> StatsReading:
    """Décode un relevé brut de l'endpoint /stats (stream=False).

    Tolère les sections vides ou absentes : un conteneur arrêté renvoie des
    dictionnaires creux, un relevé one-shot une section precpu à zéro.
    """
    mem = raw.get("memory_stats")
    mem_used: int | None = None
    mem_limit: int | None = None
    if isinstance(mem, Mapping):
        usage = mem.get("usage")
        if isinstance(usage, int):
            detail = mem.get("stats")
            # Même convention que `docker stats` : on retranche le cache
            # disque (inactive_file en cgroup v2, cache en v1).
            cache = 0
            if isinstance(detail, Mapping):
                candidate = detail.get("inactive_file", detail.get("cache"))
                if isinstance(candidate, int):
                    cache = candidate
            mem_used = max(usage - cache, 0)
        limit = mem.get("limit")
        if isinstance(limit, int):
            mem_limit = limit
    return StatsReading(
        container_id=container_id,
        cur=_decode_cpu(raw.get("cpu_stats")),
        pre=_decode_cpu(raw.get("precpu_stats")),
        mem_used_bytes=mem_used,
        mem_limit_bytes=mem_limit,
    )


def _decode_cpu(section: Any) -> CpuSample | None:
    if not isinstance(section, Mapping):
        return None
    usage = section.get("cpu_usage")
    if not isinstance(usage, Mapping):
        return None
    total = usage.get("total_usage")
    if not isinstance(total, int):
        return None
    system = section.get("system_cpu_usage")
    system_total: int | None = system if isinstance(system, int) else None
    if total == 0 and not system_total:
        # Section precpu vide (premier relevé, mode one-shot) : pas un relevé.
        return None
    online = section.get("online_cpus")
    if isinstance(online, int) and online > 0:
        online_cpus = online
    else:
        percpu = usage.get("percpu_usage")
        online_cpus = len(percpu) if isinstance(percpu, list) and percpu else 1
    return CpuSample(cpu_total=total, system_total=system_total, online_cpus=online_cpus)


class StatsCollector:
    """Mémorise le dernier relevé CPU par conteneur pour calculer les deltas.

    Aucune I/O : alimenté par la couche Docker, interrogé par l'UI. Le delta
    utilise en priorité la section precpu embarquée dans le relevé ; à
    défaut (mode one-shot), le relevé précédent mémorisé ici.
    """

    def __init__(self) -> None:
        self._last: dict[str, CpuSample] = {}

    def update(self, reading: StatsReading) -> ContainerStats:
        """Intègre un relevé et renvoie les métriques affichables."""
        prev = reading.pre if reading.pre is not None else self._last.get(reading.container_id)
        cpu = compute_cpu_percent(prev, reading.cur)
        if reading.cur is not None:
            self._last[reading.container_id] = reading.cur
        return ContainerStats(
            container_id=reading.container_id,
            cpu_percent=cpu,
            mem_used_bytes=reading.mem_used_bytes,
            mem_limit_bytes=reading.mem_limit_bytes,
        )

    def prune(self, alive_ids: set[str]) -> None:
        """Oublie les conteneurs disparus pour ne pas croître sans borne."""
        for container_id in list(self._last):
            if container_id not in alive_ids:
                del self._last[container_id]
