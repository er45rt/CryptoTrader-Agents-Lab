"""Stage1 isolation contract tests: NO strategies, market API, execution or network."""
from __future__ import annotations

import ast
from pathlib import Path
import socket
import subprocess

import pytest

from crypto_trader.validation.canonical import CanonicalError, input_manifest
from crypto_trader.validation.contracts import StudySpec, RegistryError
from crypto_trader.validation.registry import TrialRegistry

_ROOT = Path(__file__).resolve().parents[1] / "validation"


_STDLIB_ALLOWED = {
    "__future__", "dataclasses", "datetime", "decimal", "enum",
    "hashlib", "hmac", "json", "pathlib", "re", "secrets", "sqlite3",
    "threading", "time", "typing", "unicodedata", "uuid",
}
_STAGE2_MODULES = {
    "darc_frames.py", "dataset_contracts.py", "dataset_admission.py",
    "dataset_trial_binding.py",
}
_LOCAL_MODULES = {
    "canonical", "contracts", "registry", "darc_frames", "dataset_contracts",
    "dataset_admission", "dataset_trial_binding",
}
_EXTENSIONS = {
    "darc_frames.py": {"collections", "os", "stat", "struct"},
    "dataset_contracts.py": {"types"},
}


def _assert_validation_source_sandbox(tree, file_name):
    """Scan REAL AST for forbidden imports and process/network escape hatches."""
    allowed = _STDLIB_ALLOWED | _EXTENSIONS.get(file_name, set())
    parents = {
        child: node
        for node in ast.walk(tree)
        for child in ast.iter_child_nodes(node)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in allowed, (file_name, node.lineno)
                if alias.name == "os":
                    assert file_name == "darc_frames.py" and alias.asname is None, (file_name, node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                assert node.level == 1 and node.module in _LOCAL_MODULES, (file_name, node.lineno)
            else:
                assert node.module and node.module.split(".")[0] in allowed, (file_name, node.lineno)
                assert node.module != "os", (file_name, node.lineno)
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id == "os":
                assert file_name == "darc_frames.py" and node.attr == "fstat", (file_name, node.lineno)
        elif isinstance(node, ast.Name) and node.id == "os":
            owner = parents.get(node)
            assert (
                file_name == "darc_frames.py"
                and isinstance(owner, ast.Attribute)
                and owner.value is node and owner.attr == "fstat"
            ), (file_name, node.lineno)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            forbidden = {"eval", "exec", "__import__", "compile"}
            if file_name in _STAGE2_MODULES:
                forbidden |= {"getattr", "setattr", "vars", "globals", "locals"}
            assert node.func.id not in forbidden, (file_name, node.lineno)
        elif isinstance(node, ast.Name) and file_name in _STAGE2_MODULES:
            assert node.id != "__builtins__", (file_name, node.lineno)


def test_validation_package_has_only_stdlib_and_own_relative_imports():
    for file in sorted(_ROOT.glob("*.py")):
        tree = ast.parse(file.read_text(encoding="utf-8"))
        _assert_validation_source_sandbox(tree, file.name)


def test_offline_operations_work_with_network_and_process_launch_denied(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("network or subprocess forbidden in synthetic registry")

    monkeypatch.setattr(socket, "socket", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(subprocess, "Popen", denied)

    study = StudySpec(
        study_id="11111111-1111-4111-8111-111111111111",
        preregistration_hash="a" * 64,
        n_max=1,
        allowed_paths=("src/crypto_trader/paper/session.py",),
    )
    with TrialRegistry(tmp_path / "safe.sqlite") as reg:
        reg.register_study(study)
        trial = reg.allocate(
            study.study_id, {"scenario": "synthetic"},
            {"src/crypto_trader/paper/session.py": "b" * 64},
        )
        assert trial.research_label == "EXPLORATORY"
        assert reg.verify().status == "UNANCHORED"
        with pytest.raises(RegistryError):
            reg.allocate(study.study_id, {"scenario": "synthetic-2"},
                         {"src/crypto_trader/paper/session.py": "b" * 64})


def test_no_sensitive_input_manifest_paths():
    allow = ("src/crypto_trader/paper/session.py", "vault/.env")
    for path in ("vault/.env", "private.key", ".git/config", "../oops", "C:/windows/secret",
                 "src/crypto_trader/paper/session.py/../secret"):
        with pytest.raises(CanonicalError):
            input_manifest({path: "a" * 64}, allowed_paths=allow)


def test_registry_has_no_callable_strategy_or_promotion_surface():
    forbidden = {
        "trade", "place_order", "execute_order", "promote", "deploy",
        "start_strategy", "run_strategy", "run_evaluation", "connect_exchange",
    }
    assert not forbidden.intersection(set(dir(TrialRegistry)))
    assert not (_ROOT / "execution.py").exists()
    assert not (_ROOT / "runner.py").exists()


def test_os_process_execution_escape_is_statically_forbidden():
    """Even with os.fstat permitted, shell/spawn/exec APIs cannot enter validation."""
    for forbidden in ("system", "popen", "spawnv", "execv", "startfile"):
        tree=ast.parse("import os\nos."+forbidden+"('something')\n")
        unsafe=[
            n for n in ast.walk(tree)
            if isinstance(n,ast.Attribute) and isinstance(n.value,ast.Name)
            and n.value.id=="os" and n.attr!="fstat"
        ]
        assert unsafe,forbidden


def test_real_stage2_import_scanner_rejects_aliases_and_parent_packages():
    specimens=(
        ("import os as other", "darc_frames.py"),
        ("from ..paper import decisions", "dataset_admission.py"),
        ("import nonstandard_library", "dataset_contracts.py"),
        ("from os import fstat", "darc_frames.py"),
        ("from . import paper", "dataset_trial_binding.py"),
    )
    for source,filename in specimens:
        with pytest.raises(AssertionError):
            _assert_validation_source_sandbox(ast.parse(source),filename)


def test_real_stage2_scanner_accepts_only_declared_file_operations():
    source="import os\nresult = os.fstat(1)\n"
    _assert_validation_source_sandbox(ast.parse(source),"darc_frames.py")
