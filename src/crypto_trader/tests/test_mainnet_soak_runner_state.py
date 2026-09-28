"""Regression coverage for soak-runner restart classification."""

from argparse import Namespace
from datetime import timedelta
from types import SimpleNamespace

from crypto_trader.mainnet import SoakState, SoakStateStore
from crypto_trader.mainnet import soak_runner
from crypto_trader.tests import mainnet_fixtures as fx


class _Permissions:
    def to_record(self):
        return {"permission_scope": "read_only"}


class _Client:
    def verify_read_only_permissions(self):
        return _Permissions()


class _Result:
    def to_record(self):
        return {"disposition": "WOULD_REJECT"}


class _Pipeline:
    def start(self):
        return {"ready": True}

    def run_cycle(self):
        return _Result()

    def status(self):
        return {"mainnet_mutation_attempts": 0}


class _Logger:
    def __init__(self):
        self.events = []

    def write(self, event, message, **fields):
        self.events.append((event, message, fields))


def _args(*, planned_restart: bool):
    return Namespace(
        planned_restart=planned_restart,
        interval_seconds=0.0,
        max_cycles=1,
    )


def _patch_runtime(monkeypatch):
    monkeypatch.setattr(soak_runner, "_now", lambda: fx.BASE)
    monkeypatch.setattr(soak_runner, "_build_client", lambda: _Client())
    monkeypatch.setattr(
        soak_runner,
        "_build_pipeline",
        lambda client, journal, state_dir, recorder: _Pipeline(),
    )


def test_unplanned_existing_state_resets_continuous_window(monkeypatch, tmp_path):
    _patch_runtime(monkeypatch)
    store = SoakStateStore(tmp_path / "soak.json")
    store.save(
        SoakState(
            soak_start_time=fx.BASE - timedelta(hours=80),
            last_heartbeat=fx.BASE - timedelta(hours=1),
            journal_deterministic=True,
        )
    )
    logger = _Logger()

    assert soak_runner._run(_args(planned_restart=False), tmp_path, logger) == 0

    state = store.load()
    assert state is not None
    assert state.soak_start_time == fx.BASE
    assert state.counters.manual_corrective_interventions == 1
    assert state.journal_deterministic is False
    assert any(event == "unplanned_restart_recorded" for event, _, _ in logger.events)


def test_planned_existing_state_preserves_continuous_window(monkeypatch, tmp_path):
    _patch_runtime(monkeypatch)
    original_start = fx.BASE - timedelta(hours=12)
    store = SoakStateStore(tmp_path / "soak.json")
    store.save(
        SoakState(
            soak_start_time=original_start,
            last_heartbeat=fx.BASE - timedelta(minutes=1),
            journal_deterministic=True,
        )
    )
    logger = _Logger()

    assert soak_runner._run(_args(planned_restart=True), tmp_path, logger) == 0

    state = store.load()
    assert state is not None
    assert state.soak_start_time == original_start
    assert state.planned_restarts == 1
    assert state.counters.restart_recovery_exercises == 1
    assert state.counters.manual_corrective_interventions == 0
    assert state.journal_deterministic is True
    assert any(event == "planned_restart_recorded" for event, _, _ in logger.events)


def test_fresh_soak_materializes_state_without_restart_penalty(monkeypatch, tmp_path):
    _patch_runtime(monkeypatch)
    store = SoakStateStore(tmp_path / "soak.json")
    logger = _Logger()

    assert soak_runner._run(_args(planned_restart=False), tmp_path, logger) == 0

    state = store.load()
    assert state is not None
    assert state.soak_start_time == fx.BASE
    assert state.counters.manual_corrective_interventions == 0
    assert state.planned_restarts == 0
    assert state.journal_deterministic is False
