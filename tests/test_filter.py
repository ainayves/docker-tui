"""Tests du filtre incrémental et du tri de la maquette."""

from __future__ import annotations

from conftest import make_container

from dsup.docker import ContainerInfo, ContainerState, filter_containers, sort_containers


def fleet() -> list[ContainerInfo]:
    return [
        make_container(id="1" * 64, name="api", image="ghcr.io/acme/api:1.4"),
        make_container(id="2" * 64, name="worker", image="ghcr.io/acme/worker:1.4"),
        make_container(id="3" * 64, name="postgres", image="postgres:16.3"),
        make_container(
            id="4" * 64,
            name="cron",
            image="acme/cron:1.2",
            state=ContainerState.EXITED,
            exit_code=0,
        ),
        make_container(
            id="5" * 64,
            name="batch",
            image="acme/batch:0.9",
            state=ContainerState.ERROR,
            exit_code=137,
        ),
        make_container(
            id="6" * 64,
            name="agent",
            image="acme/agent:2.0",
            state=ContainerState.RESTARTING,
        ),
    ]


def test_filtre_vide_ou_blanc_rend_tout() -> None:
    containers = fleet()
    assert filter_containers(containers, "") == containers
    assert filter_containers(containers, "   ") == containers


def test_filtre_par_nom_insensible_a_la_casse() -> None:
    assert [c.name for c in filter_containers(fleet(), "WORK")] == ["worker"]


def test_filtre_par_image() -> None:
    assert [c.name for c in filter_containers(fleet(), "postgres:16")] == ["postgres"]


def test_filtre_matche_nom_ou_image() -> None:
    # "acme" n'apparaît dans aucun nom : tout doit matcher via l'image.
    names = {c.name for c in filter_containers(fleet(), "acme")}
    assert names == {"api", "worker", "cron", "batch", "agent"}


def test_filtre_sans_correspondance() -> None:
    assert filter_containers(fleet(), "zzz") == []


def test_tri_up_puis_arretes_puis_erreurs_puis_alpha() -> None:
    names = [c.name for c in sort_containers(fleet())]
    assert names == ["agent", "api", "postgres", "worker", "cron", "batch"]
