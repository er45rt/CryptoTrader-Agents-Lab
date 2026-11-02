"""B2/B3: fail before startup I/O and before an unadmitted marker."""
from dataclasses import replace
from decimal import Decimal
import pytest
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.production import ProductionStateError
from crypto_trader.production.runtime import CrewOriginAuthority
from crypto_trader.production.origin_audit import CrewOriginAudit
from crypto_trader.execution.binance_testnet import AttemptJournalRequiredError
from crypto_trader.tests.test_release_authority_report_recovery_integration import scope
from crypto_trader.tests import test_release_authority_report_recovery_integration as it
from crypto_trader.tests.production_fixtures import seed_canonical_equity_root

@pytest.mark.parametrize('broken',['checkpoint_off','authority_removed','reader_removed','strict_off','audit_required_off'])
def test_start_revalidates_checkpoint_before_any_effect(scope,broken):
    root=scope.state_dir(); proofs=root/'proofs'; proofs.mkdir()
    with SQLiteEventJournal(scope.journal_path(),venue_writes=True) as journal:
        seed_canonical_equity_root(journal); witness={}
        audit=CrewOriginAudit(proofs,key=it.KEY,journal_id=journal.journal_id(),checkpoint_reader=lambda:dict(witness))
        witness.update(audit.export_checkpoint(journal))
        runtime,_,fake=it._keyed(journal,root,CrewOriginAuthority(audit=audit),https='at_construction',require_authenticated_origin_audit=True,require_rollback_checkpoint=True)
        before=journal.revision()
        if broken=='checkpoint_off': runtime._require_rollback_checkpoint=False
        elif broken=='authority_removed': runtime._origin_authority=None
        elif broken=='reader_removed': audit._checkpoint_reader=None
        elif broken=='strict_off': runtime._require_origin_binding=False
        else: runtime._require_authenticated_origin_audit=False
        with pytest.raises(ProductionStateError,match='checkpoint'):
            runtime.start()
        assert fake.requests==[]
        assert journal.revision()==before
        assert not scope.ownership_path().exists()

@pytest.mark.parametrize('field,value',[
    ('intent_id',it.UUID(int=999)),('correlation_id',it.UUID(int=998)),
    ('strategy_id','other-strategy'),('quantity',Decimal('0.00200')),
    ('limit_price',Decimal('27100.00')),
])
def test_wrong_request_lease_creates_no_marker(scope,field,value):
    with SQLiteEventJournal(scope.journal_path(),venue_writes=True) as journal:
        runtime,fake=it._direct(scope,journal,[])
        it._hold_lease(runtime,scope,journal)
        request=replace(it.durable.execution_request(),**{field:value})
        with pytest.raises(AttemptJournalRequiredError): runtime.submit(request)
        assert it.durable.attempt_events(journal)==[]
        assert it.durable.posts(fake)==[]
