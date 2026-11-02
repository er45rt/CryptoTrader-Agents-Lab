"""Offline guarantees for the canonical on-host Testnet journal address."""
from pathlib import Path

import pytest

from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.execution import ports as authority
from crypto_trader.execution.ports import (
    CanonicalWriterScope, CanonicalWriterScopeError,
)


@pytest.fixture
def scoped_root(monkeypatch, tmp_path):
    monkeypatch.setattr(authority, "_host_authority_root", lambda: tmp_path / "host")
    return tmp_path / "host"


def scope(account="123456"):
    return CanonicalWriterScope(
        venue="binance-spot-testnet",
        environment="binance-spot-testnet",
        account_id=account,
    )


@pytest.mark.parametrize("account", ("", "0", "01", "123/../a", "1a", "1" * 21))
def test_rejects_unsafe_unverified_account_scope(account):
    with pytest.raises(CanonicalWriterScopeError):
        scope(account)


def test_one_scope_has_one_absolute_journal_address(scoped_root):
    first, second = scope(), scope()
    assert first.journal_path() == second.journal_path()
    assert first.ownership_path() == second.ownership_path()
    assert str(first.journal_path()).startswith(str(scoped_root))


def test_different_accounts_are_isolated(scoped_root):
    assert scope("123").journal_path() != scope("456").journal_path()


def test_canonical_full_journal_is_accepted_after_reopen(scoped_root):
    expected = scope().journal_path()
    expected.parent.mkdir(parents=True)
    with SQLiteEventJournal(expected, venue_writes=True) as journal:
        identity = scope().require_journal(journal)
    with SQLiteEventJournal(expected, venue_writes=True) as reopened:
        assert scope().require_journal(reopened) == identity


def test_second_full_journal_is_rejected_before_send(scoped_root, tmp_path):
    external = tmp_path / "another-state" / "journal.db"
    external.parent.mkdir(parents=True)
    with SQLiteEventJournal(external, venue_writes=True) as journal:
        with pytest.raises(CanonicalWriterScopeError, match="path differs"):
            scope().require_journal(journal)


def test_nonwriting_journal_cannot_claim_authority(scoped_root):
    expected = scope().journal_path()
    expected.parent.mkdir(parents=True)
    with SQLiteEventJournal(expected) as journal:
        with pytest.raises(CanonicalWriterScopeError):
            scope().require_journal(journal)


def test_wrong_journal_type_cannot_claim_authority(scoped_root):
    with pytest.raises(CanonicalWriterScopeError):
        scope().require_journal(object())


def test_account_with_different_case_or_venue_is_rejected():
    with pytest.raises(CanonicalWriterScopeError):
        CanonicalWriterScope("BINANCE-SPOT-TESTNET", "binance-spot-testnet", "1")

def _check_process_scope(host_root, journal_path, queue):
    """Independent Windows-spawned worker; no network, no venue transport."""
    authority._host_authority_root = lambda: Path(host_root)
    with SQLiteEventJournal(Path(journal_path), venue_writes=True) as journal:
        try:
            scope().require_journal(journal)
        except CanonicalWriterScopeError:
            queue.put("rejected")
        else:
            queue.put("accepted")


def test_spawned_processes_cannot_bind_two_different_journals(scoped_root, tmp_path):
    from multiprocessing import get_context

    canonical = scope().journal_path()
    canonical.parent.mkdir(parents=True)
    foreign = tmp_path / "isolated" / "journal.db"
    foreign.parent.mkdir(parents=True)
    for path in (canonical, foreign):
        with SQLiteEventJournal(path, venue_writes=True):
            pass
    ctx = get_context("spawn")
    queue = ctx.Queue()
    children = [
        ctx.Process(target=_check_process_scope, args=(str(scoped_root), str(p), queue))
        for p in (canonical, foreign)
    ]
    try:
        for child in children:
            child.start()
        results = [queue.get(timeout=20) for child in children]
        for child in children:
            child.join(20)
            assert child.exitcode == 0
        assert sorted(results) == ["accepted", "rejected"]
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(5)
        queue.close()
