"""Causal replay, hard validity, and centralized policy; fixtures only."""
from dataclasses import replace
from decimal import Decimal
import inspect
from types import SimpleNamespace

import pytest

from crypto_trader.tools import derivatives_schema as ds

NOW = 1_791_080_000_000


def live_oi():
    return ds.Observation("ETHUSDT", ds.Metric.CURRENT_OI, NOW - 1000, NOW, NOW,
                          ds.LIVE_PROVENANCE, ds.Status.AVAILABLE,
                          ds.OpenInterest(Decimal("123")), None, ds.CURRENT_OI_MAX_AGE_SECONDS)


def historical():
    return ds.Observation("ETHUSDT", ds.Metric.SETTLED_FUNDING, NOW - 1000, None, NOW,
                          ds.HISTORICAL_PROVENANCE, ds.Status.AVAILABLE,
                          ds.SettledFunding(Decimal("0.0001")), None, 4500)


def test_live_observation_available_at_equals_fetched_at():
    obs = live_oi()
    assert obs.available_at == obs.fetched_at
    assert obs.replay_eligible
    assert ds.strict_replay([obs], NOW) == (obs,)


def test_historical_api_has_unknown_availability_and_is_not_replay_eligible():
    obs = historical()
    assert obs.available_at is None
    assert obs.availability_provenance == "unknown_historical_api"
    assert obs.replay_eligible is False
    assert obs.to_record()["usage"] == "exploratory_only"


def test_old_event_time_unknown_availability_cannot_enter_strict_replay():
    obs = historical()
    assert obs.event_time < NOW
    assert obs.available_at is None and obs.replay_eligible is False
    assert ds.strict_replay([obs], NOW) == ()
    rejected = ds.for_strict_replay(obs, NOW)
    assert rejected.status == ds.Status.UNAVAILABLE
    assert rejected.value is None
    assert rejected.quality_reason == "unknown_replay_availability"


def test_historical_constructor_cannot_claim_live_availability():
    with pytest.raises(ValueError, match="historical_availability"):
        replace(historical(), available_at=NOW, availability_provenance=ds.LIVE_PROVENANCE)
    with pytest.raises(TypeError):
        replace(historical(), replay_eligible=True)


def test_replay_independently_rejects_constructor_bypass():
    obs = historical()
    object.__setattr__(obs, "available_at", NOW)
    object.__setattr__(obs, "availability_provenance", ds.LIVE_PROVENANCE)
    assert ds.strict_replay([obs], NOW) == ()


def test_live_unknown_availability_direct_replay_call_fails_closed():
    obs = live_oi()
    object.__setattr__(obs, "available_at", None)
    assert obs.replay_eligible is False
    rejected = ds.for_strict_replay(obs, NOW)
    assert rejected.status == ds.Status.UNAVAILABLE and rejected.value is None
    assert rejected.quality_reason == "unknown_replay_availability"
    assert rejected.available_at is None and rejected.availability_provenance == ds.HISTORICAL_PROVENANCE


def test_future_fetch_is_rejected_even_with_old_event():
    assert ds.strict_replay([live_oi()], NOW - 1) == ()


def test_replay_freshness_uses_decision_time_and_preserves_fetch_time():
    obs = live_oi()
    assert ds.strict_replay([obs], NOW + 119000) == (obs,)
    assert ds.strict_replay([obs], NOW + 120001) == ()
    assert obs.fetched_at == NOW


@pytest.mark.parametrize("hours,source,version", [
    (1, "fundingInfo", None),
    (ds.DOCUMENTED_DEFAULT_FUNDING_HOURS, "documented_default", ds.DOCUMENTED_DEFAULT_VERSION),
])
def test_metadata_without_source_event_has_explicit_replay_contract(hours, source, version):
    interval = ds.FundingInterval(hours, source, NOW, NOW, version)
    obs = ds.Observation("ETHUSDT", ds.Metric.FUNDING_INTERVAL, None, NOW, NOW,
                         ds.LIVE_PROVENANCE, ds.Status.AVAILABLE, interval, None,
                         ds.FUNDING_INFO_MAX_AGE_SECONDS)
    assert obs.replay_eligible and ds.strict_replay([obs], NOW) == (obs,)
    assert ds.strict_replay([obs], NOW + int(ds.FUNDING_INFO_MAX_AGE_SECONDS * ds.MS_PER_SECOND) + 1) == ()


def test_current_funding_replay_checks_interval_dependency_time():
    interval = ds.FundingInterval(1, "fundingInfo", NOW + 1, NOW + 1)
    value = ds.CurrentFundingContext(Decimal("0.001"), NOW + 3600000,
                                     Decimal("2000"), Decimal("1999"), NOW, interval)
    obs = replace(live_oi(), metric=ds.Metric.CURRENT_FUNDING, event_time=NOW, value=value)
    assert ds.strict_replay([obs], NOW) == ()
    assert ds.for_strict_replay(obs, NOW).quality_reason == "replay_dependency_after_decision"


def test_unversioned_default_is_not_replay_eligible():
    value = ds.FundingInterval(ds.DOCUMENTED_DEFAULT_FUNDING_HOURS, "documented_default", NOW, NOW)
    obs = replace(live_oi(), metric=ds.Metric.FUNDING_INTERVAL, event_time=None, value=value)
    assert not obs.replay_eligible
    assert ds.strict_replay([obs], NOW) == ()


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-1"])
def test_hard_invalid_value_suppresses_value(amount):
    obs = replace(live_oi(), value=ds.OpenInterest(Decimal(amount))).assess(NOW)
    assert obs.status == ds.Status.UNAVAILABLE and obs.value is None
    assert obs.to_record()["confidence_adjustment"] == "reduce"


def test_finite_extreme_has_no_invented_soft_cutoff():
    obs = replace(live_oi(), value=ds.OpenInterest(Decimal("1e100"))).assess(NOW)
    assert obs.status == ds.Status.AVAILABLE


def test_policy_budget_calculation_actual_single_symbol():
    budget = ds.request_budget(1)
    assert budget.steady_state_rpm == pytest.approx(7.2)
    assert budget.retry_steady_state_rpm == pytest.approx(14.4)
    assert budget.startup_requests == 6 and budget.startup_attempts == 12
    assert budget.healthy_worst_case_bounded_rpm == 16
    assert budget.failure_state_rpm == pytest.approx(12) and budget.admission_state_rpm == pytest.approx(12)
    assert budget.worst_case_bounded_rpm == 24
    assert budget.headroom_percent == pytest.approx(76)
    assert budget.admitted


def test_steady_state_over_24_rpm_is_rejected_without_raising_30_limit():
    assert ds.request_budget(2).admitted
    assert not ds.request_budget(3).admitted  # Healthy19.6, but failure cadence32.
    assert not ds.request_budget(4).admitted
    assert ds.request_budget(4).steady_state_rpm == pytest.approx(25.8)
    assert ds.request_budget(4).worst_case_bounded_rpm == 30
    assert ds.LOCAL_MAX_RPM == 30 and ds.MAX_STEADY_STATE_RPM == 24


@pytest.mark.parametrize("ttl", [0, -1, float("inf"), float("nan"), True])
def test_invalid_polling_policy_fails_closed(ttl):
    with pytest.raises(ValueError, match="polling_policy"):
        ds.request_budget(1, ds.PollingPolicy(current_oi=ttl))


def test_central_suspect_policy_is_named_and_invariant_based():
    assert ds.NEXT_FUNDING_MAX_INTERVAL_MULTIPLIER == 1
    assert ds.ROUNDING_HALF_UNIT == Decimal("0.5")
    assert ds.DOCUMENTED_DEFAULT_VERSION.endswith("2026-10-04")


def test_metadata_freshness_covers_nominal_connect_read_snapshot_budget():
    assert ds.PUBLIC_ENDPOINT_COUNT == len(ds.Metric)
    assert ds.IN_FLIGHT_WAIT_SECONDS >= ds.TOTAL_ATTEMPTS * ds.HTTP_TIMEOUT_SECONDS + ds.RETRY_AFTER_MAX_SECONDS
    nominal = ds.PUBLIC_ENDPOINT_COUNT * (
        ds.TOTAL_ATTEMPTS * ds.HTTP_TIMEOUT_SECONDS * ds.HTTP_CONNECT_READ_PHASES
        + ds.RETRY_AFTER_MAX_SECONDS) + ds.IN_FLIGHT_WAIT_SECONDS
    assert ds.NOMINAL_SNAPSHOT_SECONDS == nominal == 92
    assert ds.FUNDING_INFO_MAX_AGE_SECONDS == 180
    assert ds.FUNDING_INFO_MAX_AGE_SECONDS > ds.FUNDING_INFO_TTL_SECONDS + nominal


def test_freshness_skip_never_disables_future_dependency_check():
    interval = ds.FundingInterval(1, "fundingInfo", NOW + 1, NOW + 1)
    value = ds.CurrentFundingContext(Decimal("0.001"), NOW + 3600000,
                                     Decimal("2000"), Decimal("1999"), NOW, interval)
    obs = replace(live_oi(), metric=ds.Metric.CURRENT_FUNDING, event_time=NOW, value=value)
    checked = obs.assess(NOW, check_freshness=False)
    assert checked.status == ds.Status.UNAVAILABLE and checked.quality_reason == "future_funding_interval"


def test_old_interval_dependency_is_unavailable_at_delivery():
    old = NOW - int(ds.FUNDING_INFO_MAX_AGE_SECONDS * ds.MS_PER_SECOND) - 1
    interval = ds.FundingInterval(1, "fundingInfo", old, old)
    value = ds.CurrentFundingContext(Decimal("0.001"), NOW + 3600000,
                                     Decimal("2000"), Decimal("1999"), NOW, interval)
    obs = replace(live_oi(), metric=ds.Metric.CURRENT_FUNDING, event_time=NOW, value=value)
    checked = obs.assess(NOW)
    assert checked.status == ds.Status.UNAVAILABLE and checked.quality_reason == "stale_funding_interval"


@pytest.mark.parametrize("elapsed", [0, 999, 1000, 1001])
def test_current_funding_replay_expires_at_exact_scheduled_time(elapsed):
    interval = ds.FundingInterval(1, "fundingInfo", NOW, NOW)
    value = ds.CurrentFundingContext(Decimal("0.0001"), NOW + 1000,
                                     Decimal("2000"), Decimal("1999"), NOW, interval)
    obs = replace(live_oi(), metric=ds.Metric.CURRENT_FUNDING, event_time=NOW, value=value)
    checked = ds.for_strict_replay(obs, NOW + elapsed)
    if elapsed < 1000:
        assert checked.status == ds.Status.AVAILABLE and checked.value == value
    else:
        assert checked.status == ds.Status.UNAVAILABLE and checked.value is None
        assert checked.quality_reason == "stale_funding_context_after_scheduled_time"
    assert obs.value == value and obs.replay_eligible  # Immutable original remains pre-deadline evidence.


def test_schedule_expiry_precedes_generic_age_and_cannot_be_bypassed():
    interval = ds.FundingInterval(1, "fundingInfo", NOW, NOW)
    value = ds.CurrentFundingContext(Decimal("0.0001"), NOW + 1000,
                                     Decimal("2000"), Decimal("1999"), NOW, interval)
    obs = replace(live_oi(), metric=ds.Metric.CURRENT_FUNDING, event_time=NOW, value=value,
                  status=ds.Status.SUSPECT, quality_reason="funding_schedule_interval_mismatch")
    for skip in (False, True):
        checked = obs.assess(NOW + 121000, check_freshness=not skip)
        assert checked.status == ds.Status.UNAVAILABLE
        assert checked.quality_reason == "stale_funding_context_after_scheduled_time"


def test_strict_ab_gate_is_explicitly_not_yet_eligible_without_archive():
    assert ds.STRICT_AB_ADOPTION_STATUS == "NOT YET ELIGIBLE"
    assert ds.STRICT_AB_ADOPTION_REASON == "causal_archive_and_evaluation_evidence_not_verified"
    gate = ds.strict_ab_adoption_gate()
    assert gate["status"] == ds.STRICT_AB_ADOPTION_STATUS and gate["eligible"] is False
    assert gate["reason"] == ds.STRICT_AB_ADOPTION_REASON and gate["total_observation_count"] == 0
    assert list(inspect.signature(ds.strict_ab_adoption_gate).parameters) == ["observations"]


@pytest.mark.parametrize("rows", [[], [historical()], [live_oi()], [live_oi()] * 1000])
def test_history_or_live_row_count_never_proves_strict_ab_adoption(rows):
    gate = ds.strict_ab_adoption_gate(rows)
    assert gate["eligible"] is False and gate["status"] == "NOT YET ELIGIBLE"
    assert gate["counts_are_not_adoption_evidence"] is True
    for row in rows:
        assert row.to_record()["strict_ab_adoption_status"] == ds.STRICT_AB_ADOPTION_STATUS
        if row.metric in ds.HISTORICAL_METRICS:
            assert row.to_record()["usage"] == "exploratory_only" and not row.replay_eligible


def test_strict_ab_gate_counts_are_nested_and_generator_safe():
    gate = ds.strict_ab_adoption_gate(row for row in [live_oi()] * 3 + [historical()] * 3)
    assert gate["total_observation_count"] == 6
    assert gate["replay_eligible_observation_count"] == 3
    assert gate["replay_ineligible_observation_count"] == 3
    assert gate["historical_exploratory_observation_count"] == 3
    assert gate["eligible"] is False


@pytest.mark.parametrize("flag", ["archive_verified", "eligible", "duration"])
def test_caller_cannot_override_strict_ab_gate(flag):
    with pytest.raises(TypeError):
        ds.strict_ab_adoption_gate([live_oi()], **{flag: True})


def test_strict_ab_gate_result_mutation_cannot_enable_later_calls():
    gate = ds.strict_ab_adoption_gate([live_oi()])
    gate["eligible"] = True
    gate["status"] = "ELIGIBLE"
    fresh = ds.strict_ab_adoption_gate([live_oi()])
    assert fresh["eligible"] is False and fresh["status"] == ds.STRICT_AB_ADOPTION_STATUS


@pytest.mark.parametrize("row", [None, {}, SimpleNamespace(replay_eligible=True)])
def test_strict_ab_gate_rejects_untyped_or_duck_typed_evidence(row):
    with pytest.raises(TypeError, match="adoption_gate_requires_observations"):
        ds.strict_ab_adoption_gate([row])
