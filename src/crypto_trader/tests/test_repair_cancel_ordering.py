"""Offline failure boundaries for emergency fencing."""
import pytest
from types import SimpleNamespace
from crypto_trader.breakglass import BreakGlassActiveError, OwnerKind
from crypto_trader.breakglass.errors import BreakGlassIncompleteError
from crypto_trader.tests.test_breakglass_control import control, journal, command


def test_venue_exception_fences_old_writer_before_call(control, monkeypatch):
    guard = control.claim_normal_writer(owner_id='old')
    calls = []
    def fail(**kwargs):
        calls.append(control.ownership_store.read().owner_kind)
        raise RuntimeError('scripted cancel failure')
    monkeypatch.setattr(control, '_venue', SimpleNamespace(cancel_open_orders=fail))
    with pytest.raises(BreakGlassIncompleteError):
        control.handle(command('cancel_open'))
    assert calls == [OwnerKind.EMERGENCY]
    with pytest.raises(Exception):
        guard.require_current()
    assert not control.status().new_risk_permitted
    with pytest.raises(BreakGlassActiveError):
        control.claim_normal_writer(owner_id='restart')


@pytest.mark.parametrize('boundary', ['venue_death', 'append_failure', 'corrupt_high_water'])
def test_cancel_failure_recovery_and_idempotent_audit(control, journal, monkeypatch, boundary):
    guard = control.claim_normal_writer(owner_id='old')
    original = control._venue
    if boundary == 'corrupt_high_water':
        control.ownership_store._high_water_path.write_text('corrupt')
    elif boundary == 'venue_death':
        def die(**kwargs):
            assert control.ownership_store.read().owner_kind is OwnerKind.EMERGENCY
            raise KeyboardInterrupt('offline process death')
        monkeypatch.setattr(control, '_venue', SimpleNamespace(cancel_open_orders=die))
    else:
        def fail(*args, **kwargs):
            raise OSError('offline journal failure')
        monkeypatch.setattr(journal, 'append_batch', fail)
    cmd = command('cancel_open', seed=boundary)
    if boundary != 'corrupt_high_water':
        with pytest.raises((KeyboardInterrupt, BreakGlassIncompleteError)):
            control.handle(cmd)
        assert control.ownership_store.read().owner_kind is OwnerKind.EMERGENCY
        assert not control.status().new_risk_permitted
        monkeypatch.undo()
        control._venue = original
    with pytest.raises(Exception):
        guard.require_current()
    outcome = control.handle(cmd)
    assert outcome.accepted and not outcome.new_risk_permitted
    term = control.ownership_store.read().term
    assert outcome.fence_term == term
    duplicate = control.handle(cmd)
    assert duplicate.duplicate and duplicate.fence_term == term
    assert control.ownership_store.read().term == term
    assert not control.status().new_risk_permitted
    assert list(control.load_ledger().commands).count(cmd.command_id) == 1
    if boundary == 'corrupt_high_water':
        assert control.ownership_store._high_water_path.read_text() == 'corrupt'
    else:
        early = control.handle(command('restore_normal', seed=boundary + '-early'))
        assert not early.accepted
        assert control.handle(command('reconcile', seed=boundary + '-reconcile')).reconciliation_status == 'resolved'
        assert not control.handle(command('restore_normal', seed=boundary + '-unacked')).accepted
        assert control.handle(command('acknowledge', seed=boundary + '-ack')).accepted
        assert control.handle(command('restore_normal', seed=boundary + '-restore')).accepted
        control.claim_normal_writer(owner_id='recovered').require_current()
        with pytest.raises(Exception):
            guard.require_current()


def test_distinct_cancel_commands_fence_once_each(control):
    control.claim_normal_writer(owner_id='old')
    first = control.handle(command('cancel_open', seed='one'))
    second = control.handle(command('cancel_open', seed='two'))
    assert second.fence_term == first.fence_term + 1
    assert second.accepted and not second.new_risk_permitted
