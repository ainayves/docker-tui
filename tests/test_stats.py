"""Tests du calcul CPU en delta, du décodage /stats et du collecteur."""

from __future__ import annotations

from typing import Any

import pytest

from dsup.stats import (
    CpuSample,
    StatsCollector,
    StatsReading,
    compute_cpu_percent,
    parse_stats_dict,
)


def sample(total: int, system: int | None, cpus: int = 4) -> CpuSample:
    return CpuSample(cpu_total=total, system_total=system, online_cpus=cpus)


def reading(
    cur: CpuSample | None, pre: CpuSample | None = None, container_id: str = "abc"
) -> StatsReading:
    return StatsReading(container_id, cur, pre, None, None)


class TestComputeCpuPercent:
    def test_nominal(self) -> None:
        assert compute_cpu_percent(
            sample(1_000, 10_000), sample(2_000, 20_000)
        ) == pytest.approx(40.0)

    def test_premier_releve_sans_precedent(self) -> None:
        assert compute_cpu_percent(None, sample(2_000, 20_000)) is None

    def test_releve_courant_absent(self) -> None:
        assert compute_cpu_percent(sample(1_000, 10_000), None) is None

    def test_compteur_systeme_absent(self) -> None:
        assert compute_cpu_percent(sample(1_000, None), sample(2_000, 20_000)) is None
        assert compute_cpu_percent(sample(1_000, 10_000), sample(2_000, None)) is None

    def test_delta_systeme_nul(self) -> None:
        assert compute_cpu_percent(sample(1_000, 10_000), sample(2_000, 10_000)) is None

    def test_compteurs_repartis_a_zero_apres_restart(self) -> None:
        assert compute_cpu_percent(sample(9_000, 10_000), sample(100, 20_000)) is None

    def test_multiplie_par_le_nombre_de_cpus(self) -> None:
        assert compute_cpu_percent(
            sample(0, 0, cpus=8), sample(1_000, 10_000, cpus=8)
        ) == pytest.approx(80.0)


_RAW_FULL: dict[str, Any] = {
    "cpu_stats": {
        "cpu_usage": {"total_usage": 400_000_000},
        "system_cpu_usage": 8_000_000_000,
        "online_cpus": 4,
    },
    "precpu_stats": {
        "cpu_usage": {"total_usage": 200_000_000},
        "system_cpu_usage": 4_000_000_000,
        "online_cpus": 4,
    },
    "memory_stats": {
        "usage": 100_000_000,
        "stats": {"inactive_file": 20_000_000},
        "limit": 2_000_000_000,
    },
}


class TestParseStatsDict:
    def test_releve_complet_cgroup_v2(self) -> None:
        r = parse_stats_dict("abc", _RAW_FULL)
        assert r.cur == sample(400_000_000, 8_000_000_000)
        assert r.pre == sample(200_000_000, 4_000_000_000)
        assert r.mem_used_bytes == 80_000_000
        assert r.mem_limit_bytes == 2_000_000_000
        assert compute_cpu_percent(r.pre, r.cur) == pytest.approx(20.0)

    def test_precpu_vide_en_mode_one_shot(self) -> None:
        raw = dict(_RAW_FULL, precpu_stats={"cpu_usage": {"total_usage": 0}})
        assert parse_stats_dict("abc", raw).pre is None

    def test_conteneur_arrete_tout_a_none(self) -> None:
        r = parse_stats_dict("abc", {})
        assert (r.cur, r.pre, r.mem_used_bytes, r.mem_limit_bytes) == (
            None,
            None,
            None,
            None,
        )

    def test_memoire_cgroup_v1_retranche_le_cache(self) -> None:
        raw = dict(
            _RAW_FULL,
            memory_stats={
                "usage": 100_000_000,
                "stats": {"cache": 30_000_000},
                "limit": 512,
            },
        )
        assert parse_stats_dict("abc", raw).mem_used_bytes == 70_000_000

    def test_memoire_sans_detail(self) -> None:
        raw = dict(_RAW_FULL, memory_stats={"usage": 42, "limit": 512})
        r = parse_stats_dict("abc", raw)
        assert r.mem_used_bytes == 42
        assert r.mem_limit_bytes == 512

    def test_online_cpus_absent_retombe_sur_percpu(self) -> None:
        raw = {
            "cpu_stats": {
                "cpu_usage": {"total_usage": 10, "percpu_usage": [1] * 8},
                "system_cpu_usage": 100,
            }
        }
        cur = parse_stats_dict("abc", raw).cur
        assert cur is not None and cur.online_cpus == 8

    def test_online_cpus_indeterminable_vaut_un(self) -> None:
        raw = {"cpu_stats": {"cpu_usage": {"total_usage": 10}, "system_cpu_usage": 100}}
        cur = parse_stats_dict("abc", raw).cur
        assert cur is not None and cur.online_cpus == 1


class TestStatsCollector:
    def test_premier_releve_sans_cpu_puis_delta(self) -> None:
        collector = StatsCollector()
        first = collector.update(reading(sample(1_000, 10_000)))
        assert first.cpu_percent is None
        second = collector.update(reading(sample(2_000, 20_000)))
        assert second.cpu_percent == pytest.approx(40.0)

    def test_prefere_la_section_precpu_du_releve(self) -> None:
        collector = StatsCollector()
        collector.update(reading(sample(0, 0)))
        stats = collector.update(
            reading(sample(2_000, 20_000), pre=sample(1_000, 10_000))
        )
        assert stats.cpu_percent == pytest.approx(40.0)

    def test_releve_sans_cur_conserve_le_precedent(self) -> None:
        collector = StatsCollector()
        collector.update(reading(sample(1_000, 10_000)))
        blank = collector.update(reading(None))
        assert blank.cpu_percent is None
        stats = collector.update(reading(sample(2_000, 20_000)))
        assert stats.cpu_percent == pytest.approx(40.0)

    def test_deux_conteneurs_independants(self) -> None:
        collector = StatsCollector()
        collector.update(reading(sample(1_000, 10_000), container_id="a"))
        stats_b = collector.update(reading(sample(9_000, 90_000), container_id="b"))
        assert stats_b.cpu_percent is None

    def test_prune_oublie_les_disparus(self) -> None:
        collector = StatsCollector()
        collector.update(reading(sample(1_000, 10_000)))
        collector.prune(alive_ids=set())
        stats = collector.update(reading(sample(2_000, 20_000)))
        assert stats.cpu_percent is None

    def test_update_transmet_la_memoire(self) -> None:
        collector = StatsCollector()
        stats = collector.update(StatsReading("abc", None, None, 80, 2_000))
        assert stats.mem_used_bytes == 80
        assert stats.mem_limit_bytes == 2_000
