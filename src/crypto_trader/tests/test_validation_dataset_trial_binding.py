"""Zero-allocation proof; admission adapter has no registry execution authority."""
from __future__ import annotations
import ast
from pathlib import Path
import pytest

from crypto_trader.validation.dataset_trial_binding import refuse_trial_binding
from crypto_trader.validation.dataset_contracts import DatasetError
from crypto_trader.validation.dataset_admission import inspect_dataset
from test_validation_dataset_admission import sample

def test_binding_always_refused_before_registry_allocation(tmp_path):
    m=sample(tmp_path)
    report=inspect_dataset(tmp_path,m)
    class Spy:
        def allocate(self,*args,**kwargs):
            raise AssertionError("must never invoke registry.allocate")
    spy=Spy()
    refusal=refuse_trial_binding(report, study_id="synthetic", registry=spy)
    assert refusal.permits_trial_allocation is False
    assert refusal.permits_causal_use is False
    assert refusal.reason=="anchor_missing"

def test_cannot_use_mock_attestor_flag_for_admission(tmp_path):
    r=inspect_dataset(tmp_path,sample(tmp_path))
    for arg in ("pretend-valid", "revoked", "expired", None):
        refusal=refuse_trial_binding(r,study_id="synthetic",external_claim=arg)
        assert refusal.permits_trial_allocation is False
        assert refusal.reason=="anchor_missing"

def test_import_graph_no_execution_or_network():
    root=Path(__file__).resolve().parents[1]/"validation"
    names=("darc_frames.py","dataset_contracts.py","dataset_admission.py","dataset_trial_binding.py")
    banned={"socket","requests","httpx","urllib","aiohttp","ccxt","websockets",
            "subprocess","collector","execution","paper","production","risk","mainnet"}
    for name in names:
        tree=ast.parse((root/name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node,ast.Import):
                assert not any(alias.name.split(".")[0] in banned for alias in node.names)
            if isinstance(node,ast.ImportFrom):
                if node.level==0:
                    assert node.module.split(".")[0] not in banned
            if isinstance(node,ast.Attribute):
                assert node.attr not in ("allocate","place_order","execute_order","deploy")
    assert not any((root/name).read_text(encoding="utf-8").find("TrialRegistry")>=0 for name in names)
