"""Phase 5 scope boundary: the journal layer stays engine-independent and safe."""

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
persistence = PROJECT_ROOT / "src" / "crypto_trader" / "persistence"


def _files() -> list[Path]:
    return [file for file in persistence.rglob("*.py") if "__pycache__" not in file.parts]


def test_persistence_has_no_engine_or_llm_dependency():
    """Import-level check: prose may mention the engine, imports must not."""
    forbidden_imports = re.compile(
        r"^\s*(?:import|from)\s+(nautilus_trader|crewai|crypto_trader\.llm)\b", re.MULTILINE
    )
    for file in _files():
        text = file.read_text(encoding="utf-8")
        match = forbidden_imports.search(text)
        assert match is None, f"{file.name} must not import {match.group(1) if match else ''}"


def test_persistence_never_uses_pickle_or_eval():
    for file in _files():
        text = file.read_text(encoding="utf-8")
        assert "import pickle" not in text
        assert "pickle.load" not in text
        assert "eval(" not in text
        assert "exec(" not in text


def test_persistence_uses_the_phase3_registry_for_decoding():
    journal = (persistence / "journal.py").read_text(encoding="utf-8")

    assert "decode_event" in journal
    assert "get_default_registry" in journal


def test_persistence_declares_wal_and_schema_version_handling():
    journal = (persistence / "journal.py").read_text(encoding="utf-8")

    assert "PRAGMA journal_mode=WAL" in journal
    assert re.search(r"JOURNAL_SCHEMA_VERSION = \d+", journal)
    assert "JournalSchemaError" in journal
