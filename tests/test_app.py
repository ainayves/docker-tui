"""Tests de l'application Textual, pilotée avec le FakeGateway (aucun daemon)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from conftest import FakeGateway, make_container

from dsup.app import DsupApp, LogsScreen, MainScreen
from dsup.docker import ContainerInfo, ContainerState, DaemonUnreachable, DockerGateway
from dsup.widgets.container_table import ContainerTable


def fleet() -> list[ContainerInfo]:
    return [
        make_container(id="1" * 64, name="api", image="ghcr.io/acme/api:1.4"),
        make_container(
            id="2" * 64,
            name="cron",
            image="acme/cron:1.2",
            state=ContainerState.EXITED,
            exit_code=0,
        ),
        make_container(
            id="3" * 64,
            name="batch",
            image="acme/batch:0.9",
            state=ContainerState.ERROR,
            exit_code=137,
        ),
    ]


def build_app(gateway: FakeGateway) -> DsupApp:
    def factory() -> DockerGateway:
        return gateway

    return DsupApp(factory, refresh=60.0, show_all=True)


async def wait_for(condition: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while not condition():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("condition jamais remplie")
        await asyncio.sleep(0.02)


async def test_liste_affichee_et_navigation() -> None:
    gateway = FakeGateway(containers=fleet())
    app = build_app(gateway)
    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(lambda: len(app.containers) == 3)
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)
        table = app.screen.query_one(ContainerTable)
        selected = table.selected
        assert selected is not None and selected.name == "api"
        await pilot.press("j")
        selected = table.selected
        assert selected is not None and selected.name == "cron"
        await pilot.press("G")
        selected = table.selected
        assert selected is not None and selected.name == "batch"


async def test_stop_demande_confirmation() -> None:
    gateway = FakeGateway(containers=fleet())
    app = build_app(gateway)
    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(lambda: len(app.containers) == 3)
        await pilot.pause()
        await pilot.press("s")
        await pilot.press("n")  # toute autre touche annule
        await pilot.pause()
        assert gateway.actions == []
        await pilot.press("s")
        await pilot.press("y")
        await wait_for(lambda: ("stop", "1" * 64) in gateway.actions)


async def test_restart_sans_confirmation() -> None:
    gateway = FakeGateway(containers=fleet())
    app = build_app(gateway)
    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(lambda: len(app.containers) == 3)
        await pilot.pause()
        await pilot.press("r")
        await wait_for(lambda: ("restart", "1" * 64) in gateway.actions)


async def test_filtre_incremental() -> None:
    gateway = FakeGateway(containers=fleet())
    app = build_app(gateway)
    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(lambda: len(app.containers) == 3)
        await pilot.pause()
        await pilot.press("/")
        await pilot.press("b", "a", "t")
        await pilot.pause()
        assert [c.name for c in app.visible_rows()] == ["batch"]
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.visible_rows()) == 3


async def test_daemon_absent_puis_retour() -> None:
    gateway = FakeGateway(
        error=DaemonUnreachable(
            "/var/run/docker.sock : connexion refusée", hint="démarre le daemon"
        )
    )
    app = build_app(gateway)
    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(lambda: app.conn_state == "down")
        assert app.conn_error is not None
        assert "connexion refusée" in str(app.conn_error)
        # Le daemon revient : R relance immédiatement le cycle.
        gateway.error = None
        gateway.containers = fleet()
        await pilot.press("R")
        await wait_for(lambda: app.conn_state == "ok" and len(app.containers) == 3)


async def test_ecran_logs_aller_retour() -> None:
    gateway = FakeGateway(containers=fleet())
    gateway.log_lines["1" * 64] = [
        b"2026-08-07T10:41:52.000000000Z GET /health 200 1ms\n",
        b"2026-08-07T10:41:53.000000000Z WARN retry upstream\n",
    ]
    app = build_app(gateway)
    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(lambda: len(app.containers) == 3)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, LogsScreen)
        screen = app.screen
        await wait_for(lambda: len(screen._buffer) == 2)
        lines = screen._buffer.snapshot()
        assert lines[0].ts == "10:41:52"
        assert lines[1].level == 1  # WARN → orange
        await pilot.press("q")
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)


async def test_selection_stable_quand_un_conteneur_disparait() -> None:
    gateway = FakeGateway(containers=fleet())
    app = build_app(gateway)
    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(lambda: len(app.containers) == 3)
        await pilot.pause()
        await pilot.press("j")  # sélectionne cron (index 1)
        gateway.containers = [c for c in gateway.containers if c.name != "cron"]
        app.request_refresh()
        await wait_for(lambda: len(app.containers) == 2)
        await pilot.pause()
        table = app.screen.query_one(ContainerTable)
        selected = table.selected
        assert selected is not None and selected.name == "batch"
