"""Phase 13: operator break-glass command model, validation and idempotency fingerprinting."""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from crypto_trader.breakglass import (
    ACTION_TARGET_MODES,
    OperatorCommandType,
    parse_operator_command,
)
from crypto_trader.breakglass.errors import MalformedOperatorCommandError
from crypto_trader.safety import SafetyMode

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def payload(**overrides):
    base = {
        "command_type": "halt_new_risk",
        "command_id": str(uuid4()),
        "operator_label": "jamal",
        "reason": "operator requested an emergency halt",
        "source": "operator-cli",
        "requested_at": BASE.isoformat(),
    }
    base.update(overrides)
    return base


def test_every_break_glass_action_has_an_explicit_deterministic_target_mode():
    targets = {
        OperatorCommandType.HALT_NEW_RISK: SafetyMode.HALT_NEW_RISK,
        OperatorCommandType.CANCEL_OPEN: SafetyMode.CANCEL_OPEN,
        OperatorCommandType.REDUCE_ONLY: SafetyMode.REDUCE_ONLY,
        OperatorCommandType.ENTER_DEGRADED: SafetyMode.DEGRADED,
        OperatorCommandType.FORCE_RECOVERY_REQUIRED: SafetyMode.RECOVERY_REQUIRED,
    }
    for command_type, mode in targets.items():
        command = parse_operator_command(
            payload(command_type=command_type.value), delivered_at=BASE
        )
        assert command.command_type is command_type
        assert command.target_mode is mode
        assert command_type.is_break_glass_action is True
    assert ACTION_TARGET_MODES.get(OperatorCommandType.RESTORE_NORMAL) is None
    assert OperatorCommandType.RESTORE_NORMAL.is_break_glass_action is False


def test_dashed_and_underscored_command_names_are_both_accepted():
    dashed = parse_operator_command(payload(command_type="force-recovery-required"), delivered_at=BASE)
    plain = parse_operator_command(
        payload(command_type="force_recovery_required"), delivered_at=BASE
    )
    assert dashed.command_type is plain.command_type is OperatorCommandType.FORCE_RECOVERY_REQUIRED


@pytest.mark.parametrize(
    "overrides",
    [
        {"command_type": "launch_missiles"},
        {"command_type": ""},
        {"command_type": None},
        {"command_id": "not-a-uuid"},
        {"command_id": None},
        {"operator_label": ""},
        {"reason": "   "},
        {"source": ""},
        {"requested_at": "not-a-timestamp"},
        {"requested_at": "2026-01-01T12:00:00"},  # naive: never guessed
    ],
)
def test_malformed_commands_are_refused(overrides):
    with pytest.raises(MalformedOperatorCommandError):
        parse_operator_command(payload(**overrides), delivered_at=BASE)


@pytest.mark.parametrize(
    "text",
    [
        "halt because the leaked api_key is compromised",
        "secret_key rotation",
        "signature=deadbeef",
        "x-mbx-apikey exposure",
    ],
)
def test_credential_shaped_free_text_is_refused_so_it_can_never_be_journaled(text):
    with pytest.raises(MalformedOperatorCommandError):
        parse_operator_command(payload(reason=text), delivered_at=BASE)


def test_digest_is_stable_across_deliveries_and_covers_semantic_content():
    command_id = str(uuid4())
    first = parse_operator_command(payload(command_id=command_id), delivered_at=BASE)
    redelivered = parse_operator_command(
        payload(command_id=command_id, requested_at=(BASE + timedelta(hours=3)).isoformat()),
        delivered_at=BASE + timedelta(hours=3),
    )
    changed_reason = parse_operator_command(
        payload(command_id=command_id, reason="a different reason"), delivered_at=BASE
    )

    assert first.digest == redelivered.digest
    assert first.digest != changed_reason.digest
    assert len(first.digest) == 64


def test_requested_at_defaults_to_the_delivery_clock_and_is_normalized_to_utc():
    command = parse_operator_command(
        {key: value for key, value in payload().items() if key != "requested_at"},
        delivered_at=BASE,
    )
    assert command.requested_at == BASE
    assert command.requested_at.utcoffset() == timedelta(0)


def test_command_record_is_secret_free_and_carries_the_trace_identifiers():
    correlation = uuid4()
    command = parse_operator_command(
        payload(correlation_id=str(correlation)), delivered_at=BASE
    )
    record = command.to_record()

    assert record["correlation_id"] == str(correlation)
    assert record["command_digest"] == command.digest
    rendered = str(record).lower()
    for token in ("api_key", "apikey", "secret", "signature", "x-mbx"):
        assert token not in rendered


def test_causation_id_must_not_be_the_command_id():
    command_id = uuid4()
    with pytest.raises(MalformedOperatorCommandError):
        parse_operator_command(
            payload(command_id=str(command_id), causation_id=str(command_id)), delivered_at=BASE
        )


def test_command_requires_a_uuid_identity_for_idempotency():
    command = parse_operator_command(payload(command_id=str(uuid4())), delivered_at=BASE)
    assert isinstance(command.command_id, UUID)
