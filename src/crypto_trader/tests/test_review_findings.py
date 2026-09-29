from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from crypto_trader.execution import OrderLifecycleState
from crypto_trader.execution.binance_testnet.models import TestnetFill as VenueFill, TestnetOrder as VenueOrder
from crypto_trader.execution.binance_testnet.translate import testnet_order_to_phase3_events as translate
from crypto_trader.paper import PaperConfigError, PaperStateError, ScriptedSimulationRunner, TradingMode
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.tests import paper_fixtures as fx
from crypto_trader.tests.test_testnet_runtime import runtime
from crypto_trader.execution.binance_testnet import ScriptedTransport


def test_shadow_submission_requires_tested_queue_settings():
    with pytest.raises(PaperConfigError):
        fx.config(TradingMode.SHADOW, shadow_submits=True, queue_aware=False)


def test_shadow_submission_requires_a_simulator(tmp_path):
    with SQLiteEventJournal(tmp_path / 'journal.db') as journal:
        with pytest.raises(PaperStateError):
            fx.session(journal, cfg=fx.config(TradingMode.SHADOW, shadow_submits=True), decisions=fx.scripted(fx.buy_decision()), data_feed=fx.feed("2700"))


@pytest.mark.parametrize('limit,reason', [('max_open_orders','max_open_orders_exceeded'), ('max_open_positions','max_open_positions_exceeded'), ('max_symbol_exposure_quote','max_symbol_exposure_exceeded')])
def test_paper_reserves_approved_risk_before_simulation(tmp_path, limit, reason):
    value = '4000' if limit.endswith('quote') else 1
    with SQLiteEventJournal(tmp_path / 'journal.db') as journal:
        session = fx.session(journal, decisions=fx.scripted(fx.buy_decision(),fx.buy_decision()), data_feed=fx.feed('2700','2700'), simulator=ScriptedSimulationRunner(), limits_override=fx.limits(**{limit:value}))
        report = session.run()
        assert report.decisions[0].risk_decision == 'allowed'
        assert report.decisions[1].risk_decision == 'rejected'
        assert reason in report.decisions[1].risk_reason_codes


def test_paper_reservations_survive_resume(tmp_path):
    with SQLiteEventJournal(tmp_path / 'journal.db') as journal:
        first = fx.session(journal, decisions=fx.scripted(fx.buy_decision()), data_feed=fx.feed('2700'), simulator=ScriptedSimulationRunner(), limits_override=fx.limits(max_open_orders=1))
        first.run()
        second = fx.session(journal, decisions=fx.scripted(fx.buy_decision(),fx.buy_decision()), data_feed=fx.feed('2700','2700'), simulator=ScriptedSimulationRunner(), limits_override=fx.limits(max_open_orders=1))
        report = second.run(start_index=second.resume_index())
        assert 'max_open_orders_exceeded' in report.decisions[0].risk_reason_codes


def test_hold_does_not_leave_replay_pointer_behind(tmp_path):
    with SQLiteEventJournal(tmp_path / 'journal.db') as journal:
        report = fx.session(journal, decisions=fx.scripted(fx.hold_decision(),fx.buy_decision(limit_price='2800')), data_feed=fx.feed('2700','2800'), simulator=ScriptedSimulationRunner()).run()
        assert report.decisions[1].validity_disposition == 'accepted'


def venue_order(**overrides):
    return replace(VenueOrder(symbol='BTCUSDT',client_order_id='review-order',venue_order_id='7',state=OrderLifecycleState.PARTIALLY_FILLED,status='PARTIALLY_FILLED',original_quantity=Decimal('3'),executed_quantity=Decimal('1'),price=Decimal('27000'),transact_time=fx.BASE,fills=(VenueFill(price=Decimal('27000'),quantity=Decimal('1'),trade_id='1'),)),**overrides)


def test_distinct_venue_updates_do_not_collide_in_journal(tmp_path):
    correlation=uuid4()
    first=venue_order()
    second=venue_order(executed_quantity=Decimal('2'),transact_time=fx.BASE+timedelta(seconds=1),fills=(VenueFill(price=Decimal('27000'),quantity=Decimal('1'),trade_id='2'),))
    with SQLiteEventJournal(tmp_path / 'journal.db') as journal:
        port=runtime(ScriptedTransport(responses=[]),journal=journal)
        port._record(first,correlation_id=correlation)
        port._record(second,correlation_id=correlation)
        fills=[e for e in journal.read_range() if e.event_type=='execution.fill.recorded']
        assert len(fills)==2
        assert sum(e.payload.quantity for e in fills)==Decimal('2')


def test_same_trade_reordered_after_restart_is_not_counted_twice(tmp_path):
    correlation=uuid4()
    one=venue_order()
    two=venue_order(executed_quantity=Decimal('2'),transact_time=fx.BASE+timedelta(seconds=1),fills=(VenueFill(price=Decimal('27000'),quantity=Decimal('1'),trade_id='2'),one.fills[0]))
    with SQLiteEventJournal(tmp_path / 'journal.db') as journal:
        runtime(ScriptedTransport(responses=[]),journal=journal)._record(one,correlation_id=correlation)
        runtime(ScriptedTransport(responses=[]),journal=journal)._record(two,correlation_id=correlation)
        fills=[e for e in journal.read_range() if e.event_type=='execution.fill.recorded']
        assert len(fills)==2
        assert sum(e.payload.quantity for e in fills)==Decimal('2')


def test_missing_trade_identity_fails_closed():
    with pytest.raises(ValueError,match='trade'):
        translate(venue_order(fills=(VenueFill(price=Decimal('27000'),quantity=Decimal('1')),)),correlation_id=uuid4())


def test_replay_exhaustion_does_not_refresh_market_age():
    from crypto_trader.tests.test_validity_gate import observation,request,gate,BASE
    from crypto_trader.validity import ReplayMarketStateProvider
    obs=observation(0,'2700')
    provider=ReplayMarketStateProvider([obs])
    for _ in range(4):provider.advance('ETHUSDT')
    assert provider.latest('ETHUSDT').market_event_ts==obs.market_event_ts
    result=gate().evaluate(request(),market_state=provider,now=BASE+timedelta(seconds=20),capacity_available=True)
    assert result.disposition.value=='stale'


def test_repeat_risk_evaluation_refuses_duplicate():
    from crypto_trader.tests.test_risk_engine import limits,request,state
    from crypto_trader.risk import HardRiskEngine
    engine=HardRiskEngine(limits())
    req=request()
    assert engine.evaluate(req,state()).allowed
    again=engine.evaluate(req,state())
    assert not again.allowed
    assert 'duplicate_intent' in again.reason_codes
    assert len(engine.pending)==1


def test_crewbase_loads_real_project_yaml_without_provider_calls(monkeypatch):
    from crypto_trader.crew import CryptoTraderCrew
    monkeypatch.setattr(CryptoTraderCrew,'map_all_agent_variables',lambda self:None)
    monkeypatch.setattr(CryptoTraderCrew,'map_all_task_variables',lambda self:None)
    crew=CryptoTraderCrew()
    assert isinstance(crew.agents_config,dict) and 'binance_data_analyst' in crew.agents_config
    assert isinstance(crew.tasks_config,dict) and crew.tasks_config


def test_legacy_fill_identity_survives_reordered_snapshot(tmp_path):
    from crypto_trader.execution.binance_testnet.translate import legacy_fill_event_ids
    original = venue_order()
    legacy = translate(original, correlation_id=uuid4())[1]
    legacy = legacy.model_copy(update={'event_id': next(iter(legacy_fill_event_ids(original.client_order_id, '1', 1)))})
    with SQLiteEventJournal(tmp_path / 'journal.db') as journal:
        journal.append(legacy)
        runtime(ScriptedTransport(responses=[]), journal=journal)._record(
            replace(original, transact_time=fx.BASE+timedelta(seconds=2)), correlation_id=uuid4())
        fills=[e for e in journal.read_range() if e.event_type=='execution.fill.recorded']
        assert fills == [legacy]


def test_conflicting_trade_economics_fail_closed(tmp_path):
    from crypto_trader.execution import RuntimeStateError
    original=venue_order()
    conflicting=replace(original, fills=(replace(original.fills[0],price=Decimal('28000')),))
    with SQLiteEventJournal(tmp_path / 'journal.db') as journal:
        port=runtime(ScriptedTransport(responses=[]),journal=journal)
        port._record(original,correlation_id=uuid4())
        with pytest.raises(RuntimeStateError,match='conflicting economics'):
            port._record(conflicting,correlation_id=uuid4())
        assert len([e for e in journal.read_range() if e.event_type=='execution.fill.recorded'])==1


def test_opposing_reservation_survives_paper_resume(tmp_path):
    with SQLiteEventJournal(tmp_path / 'journal.db') as journal:
        fx.session(journal,decisions=fx.scripted(fx.buy_decision()),data_feed=fx.feed('2700'),simulator=ScriptedSimulationRunner()).run()
        session=fx.session(journal,decisions=fx.scripted(fx.buy_decision(),fx.sell_decision()),data_feed=fx.feed('2700','2700'),simulator=ScriptedSimulationRunner())
        report=session.run(start_index=session.resume_index())
        assert 'conflicting_intent' in report.decisions[0].risk_reason_codes


@pytest.mark.parametrize('bad',['NaN','Infinity','-Infinity'])
def test_nonfinite_paper_equity_is_rejected(bad):
    with pytest.raises(PaperConfigError):
        fx.config(starting_equity_quote=bad)


def test_d5_cli_reports_failed_orchestration_nonzero(monkeypatch,tmp_path):
    from types import SimpleNamespace
    import crypto_trader.main as main
    monkeypatch.setattr(main,'parse_arguments',lambda:SimpleNamespace(verbose=False,symbol='ETHUSDT',interval='1m',limit=10,output=str(tmp_path)))
    monkeypatch.setattr(main,'setup_environment',lambda:True)
    monkeypatch.setattr(main,'run_crew',lambda *args:None)
    with pytest.raises(SystemExit) as caught:
        main.run()
    assert caught.value.code==1


def test_halt_kill_switch_blocks_new_risk_but_preserves_emergency_lane():
    from crypto_trader.safety import SafetyController
    controller=SafetyController()
    controller.engage_kill_switch(reason_code='test',reason='offline test')
    assert not controller.allow_new_risk()
    assert not controller.allow_exposure_increase()
    assert controller.requires_cancel_open()
    # This mode capability is not authorization to submit a new-risk order.
    assert controller.permits_live_execution()


def test_soak_restart_requires_new_determinism_evidence_without_erasing_history(monkeypatch,tmp_path):
    from types import SimpleNamespace
    from crypto_trader.mainnet import SoakStateStore,SoakRecorder
    from crypto_trader.mainnet import soak_runner
    from crypto_trader.tests import mainnet_fixtures as mfx
    store=SoakStateStore(tmp_path/'soak.json')
    recorder=SoakRecorder(store,clock=lambda:fx.BASE)
    recorder.record_cycle(completed=True)
    recorder.set_journal_deterministic(True)
    before=store.load()
    monkeypatch.setattr(soak_runner,'_now',lambda:fx.BASE+timedelta(seconds=20))
    monkeypatch.setattr(soak_runner,'_build_client',lambda:SimpleNamespace(verify_read_only_permissions=mfx.restrictions))
    class Pipeline:
        def start(self):raise RuntimeError('stop offline startup after reset')
    monkeypatch.setattr(soak_runner,'_build_pipeline',lambda *args:Pipeline())
    logger=SimpleNamespace(write=lambda *args,**kwargs:None)
    assert soak_runner._run(SimpleNamespace(planned_restart=True,interval_seconds=1),tmp_path,logger)==1
    after=store.load()
    assert after.soak_start_time==before.soak_start_time
    assert after.counters.completed_pipeline_cycles==1
    assert not after.journal_deterministic
    assert 'journal_replay_determinism_not_confirmed' in recorder.exit_criteria()[1]


@pytest.mark.parametrize('mutation',['journal','capability','readback'])
def test_durable_boundary_rejects_changed_wiring_or_marker(tmp_path,monkeypatch,mutation):
    from crypto_trader.execution import NonDurableExecutionError,ExecutionCapability
    from crypto_trader.tests import testnet_fixtures as tfx
    from crypto_trader.tests.production_fixtures import build_runtime,intent
    target=intent('review-wiring')
    reference=f'ct-{target.intent_id.hex[:20]}'
    transport=ScriptedTransport(responses=[*tfx.environment_responses(),tfx.order_response(reference,status='NEW')])
    with SQLiteEventJournal(tmp_path/'journal.db') as journal, SQLiteEventJournal(tmp_path/'other.db') as other:
        port=runtime(transport,journal=journal)
        production=build_runtime(journal=journal,state_dir=tmp_path/'state',execution=port)
        production.start()
        preparation=production.prepare_intent(target)
        assert preparation.approved
        if mutation=='journal':port.journal=other
        elif mutation=='capability':port.capability=ExecutionCapability.NON_WRITING
        else:
            read=journal.read_by_id
            def corrupt(event_id):
                item=read(event_id)
                if item is not None and item.event_type=='order.submission.attempted':
                    return item.model_copy(update={'correlation_id':uuid4()})
                return item
            monkeypatch.setattr(journal,'read_by_id',corrupt)
        with pytest.raises(NonDurableExecutionError):production.submit_prepared(preparation)
        assert not [r for r in transport.requests if r.method=='POST']


@pytest.mark.parametrize('field',['max_order_notional_quote','max_symbol_exposure_quote','max_gross_exposure_quote','max_net_exposure_quote','max_strategy_exposure_quote','loss_limit_quote','max_drawdown_pct','concentration_limit_pct'])
@pytest.mark.parametrize('bad',['NaN','Infinity','-Infinity'])
def test_nonfinite_risk_configuration_is_refused(field,bad):
    from crypto_trader.risk import RiskConfigError
    with pytest.raises(RiskConfigError):fx.limits(**{field:bad})


@pytest.mark.parametrize('field',['reduce_only_mode','trading_halted'])
@pytest.mark.parametrize('bad',['false','true',0,1,None])
def test_risk_boolean_configuration_never_uses_truthiness(field,bad):
    from crypto_trader.risk import RiskConfigError
    with pytest.raises(RiskConfigError):fx.limits(**{field:bad})


@pytest.mark.parametrize('field',['secret_leaks','mainnet_mutation_attempts','manual_corrective_interventions','journal_deterministic','planned_restarts','schema_version'])
def test_missing_soak_evidence_cannot_be_replaced_with_success(field):
    from crypto_trader.mainnet import SoakState
    from crypto_trader.mainnet.errors import MainnetError
    payload=SoakState(soak_start_time=fx.BASE).to_record()
    del payload[field]
    with pytest.raises(MainnetError):SoakState.from_record(payload)


@pytest.mark.parametrize('bad',['false','true',0,1,None])
def test_soak_determinism_flag_is_strict(bad):
    from crypto_trader.mainnet import SoakState
    from crypto_trader.mainnet.errors import MainnetError
    payload=SoakState(soak_start_time=fx.BASE).to_record()
    payload['journal_deterministic']=bad
    with pytest.raises(MainnetError):SoakState.from_record(payload)
