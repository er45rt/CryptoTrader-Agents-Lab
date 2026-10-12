"""Offline Trial Registry Stage 1: research evidence only; no execution authority."""
from .contracts import IntegrityStatus, RegistryError, ResearchLabel, StudySpec, TrialReceipt, TrialStatus, Verification
from .registry import TrialRegistry
__all__ = ["TrialRegistry", "RegistryError", "StudySpec", "TrialReceipt", "TrialStatus", "IntegrityStatus", "ResearchLabel", "Verification"]
