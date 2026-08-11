"""Application Textual : boucle de rafraîchissement, écrans, actions.

L'interface ne bloque jamais : tous les appels docker-py passent par
`asyncio.to_thread` (liste, stats, inspect, actions) ou par un thread
lecteur dédié (flux de logs). Le daemon injoignable bascule l'écran en état
dégradé avec un nouvel essai automatique toutes les 5 secondes.
"""

from __future__ import annotations

import asyncio
import math
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Literal, cast

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.timer import Timer

from dsup import theme
from dsup.docker import (
    ContainerGone,
    ContainerInfo,
    DockerError,
    DockerGateway,
    context_label_from_env,
    filter_containers,
    sort_containers,
)
from dsup.stats import ContainerStats, StatsCollector
from dsup.widgets import Piece
from dsup.widgets.container_table import ContainerTable, message_placeholder
from dsup.widgets.detail_panel import DetailPanel
from dsup.widgets.log_view import LogBuffer, LogView
from dsup.widgets.status_bar import BottomBar, TopBar

RETRY_DELAY_S = 5.0
LOG_TAIL = 500
DETAIL_LOG_TAIL = 100
FLASH_DURATION_S = 4.0
WIDE_LAYOUT_MIN_COLS = 110

ConnState = Literal["connecting", "ok", "down"]

_ACTIONS_CONFIRM = {"s": "stop", "x": "kill", "d": "rm"}
_ACTIONS_DIRECT = {"r": "restart", "S": "start"}

_MAIN_KEYS: list[tuple[str, str]] = [
    ("j/k", "nav"),
    ("↵", "logs"),
    ("/", "filtre"),
    ("s", "stop"),
    ("r", "rest."),
    ("x", "kill"),
    ("d", "rm"),
    ("?", "aide"),
    ("q", "quitter"),
]
_HELP_KEYS: list[tuple[str, str]] = [
    ("S", "start"),
    ("g/G", "début/fin"),
    ("R", "rafraîchir"),
    ("Esc", "efface le filtre"),
    ("toute touche", "referme"),
]
_DOWN_KEYS: list[tuple[str, str]] = [("R", "réessayer"), ("q", "quitter")]
_LOGS_KEYS: list[tuple[str, str]] = [
    ("j/k", "défiler"),
    ("g/G", "début/fin"),
    ("/", "chercher"),
    ("f", "suivi"),
    ("w", "wrap"),
    ("↵/q", "retour"),
]


class LogStreamer:
    """Consomme un flux de logs dans un thread et alimente un LogBuffer.

    Les notifications vers l'UI sont regroupées (≥ 50 ms) pour qu'un flux
    très bavard ne noie pas la boucle d'événements.
    """

    def __init__(
        self,
        app: App[None],
        gateway: DockerGateway,
        container_id: str,
        buffer: LogBuffer,
        on_lines: Callable[[], None],
        on_end: Callable[[str | None], None],
        tail: int,
    ) -> None:
        self._app = app
        self._gateway = gateway
        self._container_id = container_id
        self._buffer = buffer
        self._on_lines = on_lines
        self._on_end = on_end
        self._tail = tail
        self._stop = threading.Event()
        self._stream: object | None = None
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"dsup-logs-{container_id[:12]}"
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        stream = self._stream
        close = getattr(stream, "close", None)
        if callable(close):
            close()

    def _run(self) -> None:
        error: str | None = None
        try:
            stream = self._gateway.stream_logs(self._container_id, tail=self._tail)
            self._stream = stream
            last_notify = 0.0
            for chunk in stream:
                if self._stop.is_set():
                    return
                self._buffer.feed(chunk)
                now = time.monotonic()
                if now - last_notify >= 0.05:
                    last_notify = now
                    self._notify(self._on_lines)
        except DockerError as err:
            error = str(err)
        if not self._stop.is_set():
            final_error = error
            self._notify(lambda: self._on_end(final_error))

    def _notify(self, callback: Callable[[], None]) -> None:
        if self._stop.is_set():
            return
        try:
            self._app.call_from_thread(callback)
        except RuntimeError:
            pass  # application déjà fermée


class DsupApp(App[None]):
    """Superviseur : un écran principal, un sous-écran logs, rien d'autre."""

    TITLE = "dsup"
    CSS = """
    Screen { background: ansi_default; color: ansi_default; }
    #body { height: 1fr; }
    """
    BINDINGS = [Binding("ctrl+c", "quit", "quitter", show=False, priority=True)]

    def __init__(
        self,
        gateway_factory: Callable[[], DockerGateway],
        *,
        refresh: float = 2.0,
        show_all: bool = False,
        initial_filter: str = "",
    ) -> None:
        super().__init__()
        self._gateway_factory = gateway_factory
        self.gateway: DockerGateway | None = None
        self.refresh_seconds = max(0.5, refresh)
        self.show_all = show_all
        self.filter_text = initial_filter
        self.context = context_label_from_env()
        self.containers: dict[str, ContainerInfo] = {}
        self.stats: dict[str, ContainerStats] = {}
        self.pending: dict[str, str] = {}
        self.conn_state: ConnState = "connecting"
        self.conn_error: DockerError | None = None
        self.spinner_frame = 0
        self._collector = StatsCollector()
        self._inspected: set[tuple[str, str]] = set()
        self._to_inspect: list[str] = []
        self._wake = asyncio.Event()
        self._retry_at = 0.0

    # ------------------------------------------------------------ cycle de vie

    def on_mount(self) -> None:
        try:
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, self.exit)
        except (NotImplementedError, RuntimeError):
            pass  # plateforme sans add_signal_handler (Windows)
        self.push_screen(MainScreen())
        self.run_worker(self._poll_loop(), group="poll", exclusive=True)
        self.set_interval(0.12, self._tick_spinner)

    def on_unmount(self) -> None:
        gateway, self.gateway = self.gateway, None
        if gateway is not None:
            threading.Thread(target=gateway.close, daemon=True).start()

    # ------------------------------------------------------------- collecte

    async def _poll_loop(self) -> None:
        while True:
            gateway = self.gateway
            if gateway is None:
                self._set_connection("connecting", None)
                try:
                    gateway = await asyncio.to_thread(self._gateway_factory)
                except DockerError as err:
                    self._set_connection("down", err)
                    await self._wait(RETRY_DELAY_S)
                    continue
                self.gateway = gateway
                self.context = gateway.context_label()
            try:
                infos = await asyncio.to_thread(
                    gateway.list_containers, self.show_all
                )
            except DockerError as err:
                self._set_connection("down", err)
                self._drop_gateway()
                await self._wait(RETRY_DELAY_S)
                continue
            self._apply_listing(infos)
            self._set_connection("ok", None)
            self.run_worker(self._enrich_cycle(), group="enrich", exclusive=True)
            await self._wait(self.refresh_seconds)

    async def _wait(self, seconds: float) -> None:
        self._retry_at = time.monotonic() + seconds
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    def request_refresh(self) -> None:
        """Déclenché par R : relance immédiatement le cycle (ou la reconnexion)."""
        self._wake.set()

    @property
    def retry_remaining(self) -> float:
        return max(0.0, self._retry_at - time.monotonic())

    def _drop_gateway(self) -> None:
        gateway, self.gateway = self.gateway, None
        if gateway is not None:
            threading.Thread(target=gateway.close, daemon=True).start()

    def _set_connection(self, state: ConnState, error: DockerError | None) -> None:
        self.conn_state = state
        self.conn_error = error
        self._refresh_screens()

    def _apply_listing(self, infos: list[ContainerInfo]) -> None:
        fresh: dict[str, ContainerInfo] = {}
        to_inspect: list[str] = []
        for info in infos:
            old = self.containers.get(info.id)
            changed = old is None or old.status_raw != info.status_raw
            if old is not None and old.state is info.state:
                info = replace(
                    info,
                    started_at=old.started_at or info.started_at,
                    restart_policy=old.restart_policy or info.restart_policy,
                    restart_count=max(old.restart_count, info.restart_count),
                    health=info.health or old.health,
                )
            if changed or (info.id, info.state.value) not in self._inspected:
                to_inspect.append(info.id)
            fresh[info.id] = info
        self.containers = fresh
        self._to_inspect = to_inspect
        self.stats = {cid: s for cid, s in self.stats.items() if cid in fresh}
        self._collector.prune(set(fresh))
        self._inspected = {key for key in self._inspected if key[0] in fresh}
        self._refresh_screens()

    async def _enrich_cycle(self) -> None:
        """Stats et inspect en tâche de fond ; la liste est déjà affichée."""
        gateway = self.gateway
        if gateway is None:
            return
        stats_ids = [
            c.id
            for c in self.containers.values()
            if c.state.is_up and c.id not in self.pending
        ]
        inspect_ids = list(self._to_inspect)
        semaphore = asyncio.Semaphore(6)

        async def sample(container_id: str) -> None:
            async with semaphore:
                try:
                    reading = await asyncio.to_thread(
                        gateway.sample_stats, container_id
                    )
                except DockerError:
                    return
            if container_id in self.containers:
                self.stats[container_id] = self._collector.update(reading)
                self._refresh_screens()

        async def enrich(container_id: str) -> None:
            async with semaphore:
                try:
                    info = await asyncio.to_thread(gateway.inspect, container_id)
                except DockerError:
                    return
            if container_id in self.containers:
                self.containers[container_id] = info
                self._inspected.add((container_id, info.state.value))
                self._refresh_screens()

        await asyncio.gather(
            *(sample(cid) for cid in stats_ids),
            *(enrich(cid) for cid in inspect_ids),
        )

    # -------------------------------------------------------------- actions

    def trigger_action(self, action: str, info: ContainerInfo) -> None:
        self.run_worker(
            self._perform_action(action, info),
            group=f"action-{info.id}",
            exclusive=True,
        )

    async def _perform_action(self, action: str, info: ContainerInfo) -> None:
        gateway = self.gateway
        if gateway is None:
            return
        methods: dict[str, Callable[[str], None]] = {
            "start": gateway.start,
            "stop": gateway.stop,
            "restart": gateway.restart,
            "kill": gateway.kill,
            "rm": gateway.remove,
        }
        self.pending[info.id] = action
        self._refresh_screens()
        try:
            await asyncio.to_thread(methods[action], info.id)
        except ContainerGone:
            pass
        except DockerError as err:
            self._flash_error(err)
        finally:
            self.pending.pop(info.id, None)
            self._inspected = {k for k in self._inspected if k[0] != info.id}
            self.request_refresh()

    # ----------------------------------------------------------------- vues

    def visible_rows(self) -> list[ContainerInfo]:
        return sort_containers(
            filter_containers(list(self.containers.values()), self.filter_text)
        )

    def counters(self) -> tuple[int, int, int]:
        up = stopped = error = 0
        for info in self.containers.values():
            if info.state.is_up:
                up += 1
            elif info.state.value == "error":
                error += 1
            else:
                stopped += 1
        return up, stopped, error

    def start_log_stream(
        self,
        container_id: str,
        buffer: LogBuffer,
        on_lines: Callable[[], None],
        on_end: Callable[[str | None], None],
        tail: int,
    ) -> LogStreamer | None:
        gateway = self.gateway
        if gateway is None:
            return None
        streamer = LogStreamer(
            self, gateway, container_id, buffer, on_lines, on_end, tail
        )
        streamer.start()
        return streamer

    def _tick_spinner(self) -> None:
        if self.conn_state != "ok":
            self.spinner_frame += 1
            self._refresh_screens()

    def _refresh_screens(self) -> None:
        for screen in self.screen_stack:
            if isinstance(screen, MainScreen):
                screen.refresh_data()
            elif isinstance(screen, LogsScreen):
                screen.refresh_bars()

    def _flash_error(self, err: DockerError) -> None:
        for screen in self.screen_stack:
            if isinstance(screen, MainScreen):
                screen.flash_error(err)


class MainScreen(Screen[None]):
    """Écran principal : table, panneau détail en grand terminal, barres."""

    def __init__(self) -> None:
        super().__init__()
        self._mode: Literal["normal", "filter", "confirm", "help"] = "normal"
        self._confirm: tuple[str, ContainerInfo] | None = None
        self._flash_active = False
        self._flash_timer: Timer | None = None
        self._detail_buffer = LogBuffer(DETAIL_LOG_TAIL * 3)
        self._detail_streamer: LogStreamer | None = None
        self._detail_id: str | None = None
        self._detail_timer: Timer | None = None
        self._detail_live = False

    @property
    def dsup(self) -> DsupApp:
        return cast(DsupApp, self.app)

    def compose(self) -> ComposeResult:
        yield TopBar()
        with Horizontal(id="body"):
            yield ContainerTable()
            yield DetailPanel()
        yield BottomBar()

    def on_mount(self) -> None:
        self.set_interval(0.5, self.refresh_data)
        self.refresh_data()

    def on_unmount(self) -> None:
        self._stop_detail_stream()

    def on_resize(self, event: events.Resize) -> None:
        panel = self.query_one(DetailPanel)
        wide = self.size.width >= WIDE_LAYOUT_MIN_COLS
        panel.display = wide
        if wide:
            panel.styles.width = max(34, self.size.width - 80)
        self.refresh_data()

    # ---------------------------------------------------------------- rendu

    def refresh_data(self) -> None:
        if not self.is_mounted:
            return
        app = self.dsup
        table = self.query_one(ContainerTable)
        remote = app.context != "local"
        top_left: list[Piece] = [
            ("dsup", theme.S_BOLD),
            (" · ", theme.TEXT),
            (app.context, theme.S_BLUE if remote else theme.TEXT),
        ]
        if app.conn_state == "ok":
            count = len(app.containers)
            label = "conteneur" if count < 2 else "conteneurs"
            top_right: list[Piece] = [
                (f"maj. {app.refresh_seconds:g} s · {count} {label}", theme.S_FAINT)
            ]
            rows = app.visible_rows()
            table.update_rows(rows, app.stats, app.pending)
            if rows:
                table.set_placeholder(None)
            elif app.filter_text:
                table.set_placeholder(
                    message_placeholder(
                        "◌",
                        theme.S_GREY,
                        "aucun conteneur ne correspond au filtre",
                        [f"filtre : {app.filter_text} · Esc pour effacer"],
                    )
                )
            else:
                lines = ["docker run -d IMAGE pour en lancer un — dsup le verra tout seul"]
                if not app.show_all:
                    lines.append("conteneurs arrêtés masqués · lance dsup --all pour les voir")
                table.set_placeholder(
                    message_placeholder("◌", theme.S_GREY, "aucun conteneur", lines)
                )
        elif app.conn_state == "connecting":
            top_right = [("—", theme.S_FAINT)]
            frame = theme.SPINNER[app.spinner_frame % len(theme.SPINNER)]
            table.set_placeholder(
                message_placeholder(
                    frame,
                    theme.S_ORANGE,
                    f"connexion à {app.context} …",
                    ["docker ping"],
                )
            )
        else:
            top_right = [("—", theme.S_FAINT)]
            err = app.conn_error
            lines = [str(err)] if err else []
            if err is not None and err.hint:
                lines.append(err.hint)
            remaining = max(1, math.ceil(app.retry_remaining))
            table.set_placeholder(
                message_placeholder(
                    "✖",
                    theme.S_RED,
                    "docker non joignable",
                    lines,
                    footer=f"nouvel essai auto dans {remaining} s · R réessayer maintenant",
                )
            )
        self.query_one(TopBar).set_content(top_left, top_right)
        self._update_bottom()
        self._sync_detail()

    def _counter_pieces(self) -> list[Piece]:
        up, stopped, error = self.dsup.counters()
        pieces: list[Piece] = [
            (f"● {up}", theme.S_GREEN),
            (" ", theme.TEXT),
            (f"◌ {stopped}", theme.S_FAINT),
        ]
        if error:
            pieces.append((" ", theme.TEXT))
            pieces.append((f"✖ {error}", theme.S_RED))
        return pieces

    def _update_bottom(self) -> None:
        if self._mode in ("confirm", "help") or self._flash_active:
            return
        bar = self.query_one(BottomBar)
        app = self.dsup
        if self._mode == "filter":
            bar.show_prompt("/", app.filter_text)
            return
        if app.conn_state == "down":
            bar.show_keys(_DOWN_KEYS, right=self._counter_pieces())
            return
        prefix: list[Piece] = []
        if app.filter_text:
            prefix = [(f"/{app.filter_text}", theme.S_BLUE), ("  ", theme.TEXT)]
        bar.show_keys(_MAIN_KEYS, right=self._counter_pieces(), prefix=prefix)

    # -------------------------------------------------------------- clavier

    def on_key(self, event: events.Key) -> None:
        char = event.character
        key = event.key
        if self._mode == "confirm":
            self._handle_confirm(char)
            event.stop()
            return
        if self._mode == "help":
            self._mode = "normal"
            self._update_bottom()
            event.stop()
            return
        if self._mode == "filter":
            self._handle_filter_key(key, char)
            event.stop()
            return
        self._handle_normal_key(key, char, event)

    def _handle_confirm(self, char: str | None) -> None:
        confirm, self._confirm = self._confirm, None
        self._mode = "normal"
        if confirm is not None and char == "y":
            action, info = confirm
            self.dsup.trigger_action(action, info)
        self._update_bottom()

    def _handle_filter_key(self, key: str, char: str | None) -> None:
        app = self.dsup
        if key == "escape":
            app.filter_text = ""
            self._mode = "normal"
        elif key == "enter":
            self._mode = "normal"
        elif key == "backspace":
            app.filter_text = app.filter_text[:-1]
        elif char and char.isprintable():
            app.filter_text += char
        self.refresh_data()

    def _handle_normal_key(
        self, key: str, char: str | None, event: events.Key
    ) -> None:
        app = self.dsup
        table = self.query_one(ContainerTable)
        if char == "j" or key == "down":
            table.move(1)
        elif char == "k" or key == "up":
            table.move(-1)
        elif char == "g":
            table.select_first()
        elif char == "G":
            table.select_last()
        elif char == "/":
            self._mode = "filter"
        elif key == "enter":
            selected = table.selected
            if selected is not None and app.conn_state == "ok":
                app.push_screen(LogsScreen(selected))
        elif char in _ACTIONS_CONFIRM:
            selected = table.selected
            if selected is not None and app.conn_state == "ok":
                action = _ACTIONS_CONFIRM[char]
                self._confirm = (action, selected)
                self._mode = "confirm"
                self._end_flash()
                self.query_one(BottomBar).show_confirm(action, selected.name)
        elif char in _ACTIONS_DIRECT:
            selected = table.selected
            if selected is not None and app.conn_state == "ok":
                app.trigger_action(_ACTIONS_DIRECT[char], selected)
        elif char == "R":
            app.request_refresh()
        elif char == "?":
            self._mode = "help"
            self.query_one(BottomBar).show_keys(_HELP_KEYS)
        elif char == "q":
            app.exit()
        elif key == "escape":
            if app.filter_text:
                app.filter_text = ""
            else:
                app.exit()
        else:
            return
        event.stop()
        if self._mode == "normal":
            self.refresh_data()

    # -------------------------------------------------------------- messages

    def flash_error(self, err: DockerError) -> None:
        if not self.is_mounted or self._mode == "confirm":
            return
        self._flash_active = True
        self.query_one(BottomBar).show_message(str(err), err.hint)
        if self._flash_timer is not None:
            self._flash_timer.stop()
        self._flash_timer = self.set_timer(FLASH_DURATION_S, self._end_flash)

    def _end_flash(self) -> None:
        self._flash_active = False
        if self._flash_timer is not None:
            self._flash_timer.stop()
            self._flash_timer = None
        self._update_bottom()

    # -------------------------------------------------------- panneau détail

    def _sync_detail(self) -> None:
        panel = self.query_one(DetailPanel)
        if not panel.display:
            self._stop_detail_stream()
            return
        app = self.dsup
        selected = self.query_one(ContainerTable).selected
        if selected is None or app.conn_state != "ok":
            self._stop_detail_stream()
            panel.set_data(None, None, (), False)
            return
        lines = (
            self._detail_buffer.snapshot()[-DETAIL_LOG_TAIL:]
            if selected.id == self._detail_id
            else ()
        )
        panel.set_data(selected, app.stats.get(selected.id), lines, self._detail_live)
        if selected.id != self._detail_id:
            self._detail_id = selected.id
            if self._detail_timer is not None:
                self._detail_timer.stop()
            self._detail_timer = self.set_timer(0.3, self._restart_detail_stream)

    def _restart_detail_stream(self) -> None:
        self._stop_detail_stream(keep_id=True)
        if self._detail_id is None:
            return
        self._detail_buffer = LogBuffer(DETAIL_LOG_TAIL * 3)
        self._detail_streamer = self.dsup.start_log_stream(
            self._detail_id,
            self._detail_buffer,
            self._on_detail_lines,
            self._on_detail_end,
            DETAIL_LOG_TAIL,
        )
        self._detail_live = self._detail_streamer is not None
        self._on_detail_lines()

    def _stop_detail_stream(self, keep_id: bool = False) -> None:
        if self._detail_streamer is not None:
            self._detail_streamer.stop()
            self._detail_streamer = None
        self._detail_live = False
        if not keep_id:
            self._detail_id = None

    def _on_detail_lines(self) -> None:
        if not self.is_mounted:
            return
        panel = self.query_one(DetailPanel)
        selected = self.query_one(ContainerTable).selected
        if panel.display and selected is not None and selected.id == self._detail_id:
            panel.set_data(
                selected,
                self.dsup.stats.get(selected.id),
                self._detail_buffer.snapshot()[-DETAIL_LOG_TAIL:],
                self._detail_live,
            )

    def _on_detail_end(self, error: str | None) -> None:
        self._detail_live = False
        self._on_detail_lines()


class LogsScreen(Screen[None]):
    """Sous-écran logs : suivi temps réel, recherche dans le tampon, wrap."""

    def __init__(self, info: ContainerInfo) -> None:
        super().__init__()
        self._info = info
        self._buffer = LogBuffer(2000)
        self._streamer: LogStreamer | None = None
        self._mode: Literal["normal", "search"] = "normal"
        self._search_text = ""
        self._note: str | None = None

    @property
    def dsup(self) -> DsupApp:
        return cast(DsupApp, self.app)

    def compose(self) -> ComposeResult:
        yield TopBar()
        yield LogView()
        yield BottomBar()

    def on_mount(self) -> None:
        view = self.query_one(LogView)
        view.attach(self._buffer)
        self._streamer = self.dsup.start_log_stream(
            self._info.id, self._buffer, self._on_lines, self._on_end, LOG_TAIL
        )
        if self._streamer is None:
            self._note = "docker non joignable"
        self.set_interval(1.0, self.refresh_bars)
        self.refresh_bars()

    def on_unmount(self) -> None:
        if self._streamer is not None:
            self._streamer.stop()
            self._streamer = None

    def _on_lines(self) -> None:
        if self.is_mounted:
            self.query_one(LogView).on_new_lines()
            self.refresh_bars()

    def _on_end(self, error: str | None) -> None:
        self._note = error if error else "flux terminé"
        self._streamer = None
        if self.is_mounted:
            self.refresh_bars()
            self.query_one(LogView).refresh()

    def refresh_bars(self) -> None:
        if not self.is_mounted:
            return
        app = self.dsup
        view = self.query_one(LogView)
        top_left: list[Piece] = [
            ("logs", theme.S_BOLD),
            (f" · {self._info.name} ", theme.TEXT),
            (f"· {self._info.image}", theme.S_GREY),
        ]
        top_right: list[Piece] = []
        if self._note is not None:
            top_right.append((f"{self._note} · ", theme.S_FAINT))
        elif view.follow:
            top_right.append(("suivi ● · ", theme.S_GREEN))
        else:
            top_right.append(("suivi ○ · ", theme.S_FAINT))
        count = len(self._buffer)
        top_right.append((f"{count} ligne{'s' if count > 1 else ''}", theme.S_FAINT))
        self.query_one(TopBar).set_content(top_left, top_right)

        bar = self.query_one(BottomBar)
        current = self.dsup.containers.get(self._info.id, self._info)
        state_text, state_style = theme.state_display(current.state, current.exit_code)
        right: list[Piece] = [
            (f"{current.name} · ", theme.TEXT),
            (state_text, state_style),
        ]
        if self._mode == "search":
            found = view.match_count
            prompt_right: list[Piece] = [
                (f"{found} résultat{'s' if found > 1 else ''}", theme.TEXT)
            ]
            bar.show_prompt("/", self._search_text, right=prompt_right)
            return
        keys = list(_LOGS_KEYS)
        if self._search_text:
            keys.insert(3, ("n/N", "suivant/préc."))
        bar.show_keys(keys, right=right)

    def on_key(self, event: events.Key) -> None:
        view = self.query_one(LogView)
        char = event.character
        key = event.key
        if self._mode == "search":
            if key == "escape":
                self._search_text = ""
                view.set_search("")
                self._mode = "normal"
            elif key == "enter":
                self._mode = "normal"
            elif key == "backspace":
                self._search_text = self._search_text[:-1]
                view.set_search(self._search_text)
            elif char and char.isprintable():
                self._search_text += char
                view.set_search(self._search_text)
            self.refresh_bars()
            event.stop()
            return
        if char == "j" or key == "down":
            view.scroll_by(1)
        elif char == "k" or key == "up":
            view.scroll_by(-1)
        elif char == "g":
            view.goto_top()
        elif char == "G":
            view.goto_end()
        elif char == "f":
            view.toggle_follow()
        elif char == "w":
            view.toggle_wrap()
        elif char == "/":
            self._mode = "search"
        elif char == "n":
            view.next_match(1)
        elif char == "N":
            view.next_match(-1)
        elif key in ("enter", "escape") or char == "q":
            self.app.pop_screen()
            return
        else:
            return
        event.stop()
        self.refresh_bars()
