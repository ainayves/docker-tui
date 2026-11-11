"""Tests du tampon de logs et de la sélection de lignes dans LogView."""

from __future__ import annotations

import asyncio

from textual import events
from textual.app import App, ComposeResult

from dsup.widgets.log_view import LogBuffer, LogView
from dsup.widgets.status_bar import BottomBar


def lignes(*textes: str) -> bytes:
    return "".join(f"{t}\n" for t in textes).encode()


class _Banc(App[None]):
    """Une LogView sur une barre d'une ligne, où l'on peut glisser hors du panneau."""

    def __init__(self, buffer: LogBuffer) -> None:
        super().__init__()
        self.buffer = buffer
        self.copies: list[tuple[str, int]] = []

    def compose(self) -> ComposeResult:
        yield LogView()
        yield BottomBar()

    def on_mount(self) -> None:
        self.query_one(LogView).attach(self.buffer)

    def on_log_view_selected(self, message: LogView.Selected) -> None:
        self.copies.append((message.text, message.lines))


# ------------------------------------------------------------------ tampon


def test_tampon_compte_les_lignes_evincees() -> None:
    buffer = LogBuffer(3)
    buffer.feed(lignes("a", "b"))
    assert buffer.dropped == 0
    buffer.feed(lignes("c", "d", "e"))
    assert buffer.dropped == 2
    first, kept = buffer.window()
    assert first == 2
    assert [line.text for line in kept] == ["c", "d", "e"]


def test_ligne_partielle_ne_change_pas_la_version() -> None:
    buffer = LogBuffer()
    buffer.feed(b"debut sans fin")
    assert buffer.version == 0 and len(buffer) == 0
    buffer.feed(b" de ligne\n")
    assert buffer.version == 1
    assert buffer.snapshot()[0].text == "debut sans fin de ligne"


# --------------------------------------------------------------- sélection


async def test_selection_part_de_la_derniere_ligne_et_s_etend() -> None:
    buffer = LogBuffer()
    buffer.feed(lignes("un", "deux", "trois"))
    app = _Banc(buffer)
    async with app.run_test(size=(40, 10)) as pilot:
        view = app.query_one(LogView)
        assert view.start_selection()
        assert view.follow is False
        assert view.selected_text() == ("trois", 1)
        view.move_cursor(-1)
        await pilot.pause()
        assert view.selected_text() == ("deux\ntrois", 2)
        view.move_cursor(-10)  # borné à la première ligne
        assert view.selection_size == 3


async def test_lignes_copiees_entieres_meme_tronquees_ou_repliees() -> None:
    longue = "x" * 100
    buffer = LogBuffer()
    buffer.feed(
        lignes("2026-08-07T10:41:52.000000000Z " + longue, "fin")
    )
    app = _Banc(buffer)
    async with app.run_test(size=(30, 10)) as pilot:
        view = app.query_one(LogView)
        for wrap in (False, True):
            view.wrap = wrap
            assert view.start_selection()
            view.select_all()
            await pilot.pause()
            assert view.selected_text() == (f"10:41:52 {longue}\nfin", 2)
            view.end_selection()


async def test_fin_de_selection_rend_le_suivi() -> None:
    buffer = LogBuffer()
    buffer.feed(lignes("a", "b"))
    app = _Banc(buffer)
    async with app.run_test(size=(40, 10)):
        view = app.query_one(LogView)
        assert view.follow
        view.start_selection()
        view.end_selection()
        assert view.follow and not view.selecting

        view.toggle_follow()  # suivi coupé avant : il le reste après
        view.start_selection()
        view.end_selection()
        assert not view.follow


async def test_tampon_vide_pas_de_selection() -> None:
    app = _Banc(LogBuffer())
    async with app.run_test(size=(40, 10)):
        view = app.query_one(LogView)
        assert view.start_selection() is False
        assert not view.selecting


async def test_selection_reste_sur_son_texte_quand_le_tampon_tourne() -> None:
    """Tampon plein : les indices glissent, la sélection doit suivre son texte."""
    buffer = LogBuffer(6)
    buffer.feed(lignes(*(f"l{i}" for i in range(6))))
    app = _Banc(buffer)
    async with app.run_test(size=(40, 4)) as pilot:
        view = app.query_one(LogView)
        view.start_selection()  # curseur sur l5
        view.move_cursor(-1)
        await pilot.pause()
        assert view.selected_text() == ("l4\nl5", 2)
        top_before = view.render_line(0).text

        buffer.feed(lignes("l6", "l7"))  # évince l0 et l1
        view.on_new_lines()
        await pilot.pause()
        assert view.selected_text() == ("l4\nl5", 2)
        # La vue figée ne glisse pas non plus.
        assert view.render_line(0).text == top_before


async def test_selection_evincee_ne_copie_rien() -> None:
    buffer = LogBuffer(3)
    buffer.feed(lignes("a", "b", "c"))
    app = _Banc(buffer)
    async with app.run_test(size=(40, 10)) as pilot:
        view = app.query_one(LogView)
        view.start_selection()
        view.cursor_to_start()
        buffer.feed(lignes("d", "e", "f", "g"))
        view.on_new_lines()
        await pilot.pause()
        assert view.selected_text() == ("", 0)


# ------------------------------------------------------------------ souris


async def test_glisser_sur_une_ligne_copie_au_caractere_pres() -> None:
    buffer = LogBuffer()
    buffer.feed(lignes("alpha beta", "gamma delta"))
    app = _Banc(buffer)
    async with app.run_test(size=(40, 10)) as pilot:
        await pilot.mouse_down(LogView, offset=(6, 0))
        await pilot.hover(LogView, offset=(9, 0))
        await pilot.mouse_up(LogView, offset=(9, 0))
        assert app.copies == [("beta", 1)]
        view = app.query_one(LogView)
        assert not view.selecting and view.follow  # le suivi reprend


async def test_glisser_sur_plusieurs_lignes() -> None:
    buffer = LogBuffer()
    buffer.feed(lignes("alpha beta", "gamma delta", "epsilon"))
    app = _Banc(buffer)
    async with app.run_test(size=(40, 10)) as pilot:
        await pilot.mouse_down(LogView, offset=(6, 0))
        await pilot.hover(LogView, offset=(4, 1))
        await pilot.mouse_up(LogView, offset=(4, 1))
        assert app.copies == [("beta\ngamma", 2)]


async def test_glisser_vers_le_haut_et_au_dela_du_texte() -> None:
    buffer = LogBuffer()
    buffer.feed(lignes("alpha beta", "gamma"))
    app = _Banc(buffer)
    async with app.run_test(size=(40, 10)) as pilot:
        await pilot.mouse_down(LogView, offset=(30, 1))  # après « gamma »
        await pilot.hover(LogView, offset=(6, 0))
        await pilot.mouse_up(LogView, offset=(6, 0))
        assert app.copies == [("beta\ngamma", 2)]


async def test_simple_clic_ne_copie_rien() -> None:
    buffer = LogBuffer()
    buffer.feed(lignes("alpha"))
    app = _Banc(buffer)
    async with app.run_test(size=(40, 10)) as pilot:
        await pilot.click(LogView, offset=(2, 0))
        view = app.query_one(LogView)
        assert app.copies == []
        assert view.follow and not view.selecting


async def test_glisser_hors_du_panneau_fait_defiler() -> None:
    buffer = LogBuffer()
    buffer.feed(lignes(*(f"l{i:02d}" for i in range(40))))
    app = _Banc(buffer)
    async with app.run_test(size=(40, 6)) as pilot:  # 5 lignes visibles
        view = app.query_one(LogView)
        view.goto_top()
        await pilot.mouse_down(LogView, offset=(0, 0))
        await pilot.hover(LogView, offset=(0, 5))  # sur la barre, sous le panneau
        await asyncio.sleep(0.4)
        await pilot.mouse_up(LogView, offset=(0, 5))
        (text, count), = app.copies
        assert text.startswith("l00\nl01")
        assert count > 5


async def test_molette_fait_defiler_et_coupe_le_suivi() -> None:
    buffer = LogBuffer()
    buffer.feed(lignes(*(f"l{i:02d}" for i in range(40))))
    app = _Banc(buffer)
    async with app.run_test(size=(40, 6)) as pilot:
        view = app.query_one(LogView)
        assert view.render_line(4).text.startswith("l39")
        app.screen._forward_event(
            events.MouseScrollUp(None, 1, 1, 0, 0, 0, False, False, False, 1, 1)
        )
        await pilot.pause()
        assert not view.follow
        assert view.render_line(4).text.startswith("l36")
