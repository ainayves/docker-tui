"""Tests du parsing des arguments de la ligne de commande."""

from __future__ import annotations

import pytest

from dsup import __version__
from dsup.cli import build_parser, main


def test_valeurs_par_defaut() -> None:
    args = build_parser().parse_args([])
    assert args.refresh == 2.0
    assert args.all is False
    assert args.filter == ""


def test_options_explicites() -> None:
    args = build_parser().parse_args(
        ["--refresh", "5", "--all", "--filter", "api"]
    )
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
