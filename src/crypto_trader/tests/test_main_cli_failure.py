"""Regression coverage for CLI failure exit semantics (review finding D5)."""

from types import SimpleNamespace

import pytest

from crypto_trader import main


def _args():
    return SimpleNamespace(
        symbol="ETHUSDT",
        interval="4h",
        limit=72,
        output="output",
        verbose=False,
    )


def test_run_exits_nonzero_when_environment_setup_fails(monkeypatch):
    monkeypatch.setattr(main, "parse_arguments", _args)
    monkeypatch.setattr(main, "setup_environment", lambda: False)

    with pytest.raises(SystemExit) as exc:
        main.run()

    assert exc.value.code == 1


def test_run_exits_nonzero_when_input_validation_fails(monkeypatch):
    monkeypatch.setattr(main, "parse_arguments", _args)
    monkeypatch.setattr(main, "setup_environment", lambda: True)
    monkeypatch.setattr(main, "validate_inputs", lambda config: False)

    with pytest.raises(SystemExit) as exc:
        main.run()

    assert exc.value.code == 1


def test_run_exits_nonzero_when_crew_execution_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "parse_arguments", _args)
    monkeypatch.setattr(main, "setup_environment", lambda: True)
    monkeypatch.setattr(main, "validate_inputs", lambda config: True)
    monkeypatch.setattr(main, "prepare_output_directory", lambda config: tmp_path)
    monkeypatch.setattr(
        main,
        "create_inputs",
        lambda config: {
            "target_symbol": config["target_symbol"],
            "kline_interval": config["kline_interval"],
            "kline_limit": config["kline_limit"],
            "current_datetime": "2026-01-01 00:00:00",
            "filename_datetime": "20260101_000000",
        },
    )
    monkeypatch.setattr(main, "run_crew", lambda inputs, output_dir: None)

    with pytest.raises(SystemExit) as exc:
        main.run()

    assert exc.value.code == 1


def test_run_crew_converts_crew_exception_to_failed_result(monkeypatch, tmp_path):
    class ExplodingCrew:
        def kickoff(self, *, inputs):
            raise RuntimeError("synthetic crew failure")

    class CrewFactory:
        def crew(self):
            return ExplodingCrew()

    monkeypatch.setattr(main, "CryptoTraderCrew", CrewFactory)

    result = main.run_crew(
        {
            "target_symbol": "ETHUSDT",
            "filename_datetime": "20260101_000000",
        },
        tmp_path,
    )

    assert result is None
