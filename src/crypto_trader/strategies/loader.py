"""Install / select strategies without touching the engine.

A strategy is referenced by a spec:
  * "path/to/file.py"             -> the single Strategy subclass defined in that file
  * "path/to/file.py:ClassName"   -> that class
  * "package.module:ClassName"    -> an importable class
  * "freqtrade:path/to/File.py:ClassName" -> a Freqtrade IStrategy via the adapter (see freqtrade_adapter)

Installing = putting a .py file in a strategies folder. Removing = deleting it. Swapping = passing
another spec. The engine package is never edited.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import inspect
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .api import Strategy, StrategyError

__all__ = ["LoadedStrategy", "load_strategy", "list_strategies"]


@dataclass(frozen=True)
class LoadedStrategy:
    spec: str
    cls: type
    source_path: str | None
    source_sha256: str | None

    def instantiate(self, **params: Any) -> Strategy:
        return self.cls(**params)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _strategy_classes(module: Any) -> list[type]:
    found = []
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, Strategy) and obj is not Strategy and obj.__module__ == module.__name__:
            found.append(obj)
    return found


def _load_file(path: Path) -> Any:
    if not path.is_file() or path.suffix != ".py":
        raise StrategyError(f"strategy file not found: {path}")
    digest = _sha256(path)[:16]
    name = f"ct_user_strategy_{path.stem}_{digest}"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise StrategyError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(name, None)
        raise StrategyError(f"strategy file {path} failed to import: {type(exc).__name__}: {exc}") from exc
    return module


def load_strategy(spec: str) -> LoadedStrategy:
    if spec.startswith("freqtrade:"):
        from .freqtrade_adapter import load_freqtrade_strategy

        return load_freqtrade_strategy(spec[len("freqtrade:"):])
    target, class_name = _split_spec(spec)
    path = Path(target)
    source_path = None
    source_sha = None
    if target.endswith(".py"):
        module = _load_file(path)
        source_path, source_sha = str(path.resolve()), _sha256(path)
    else:
        try:
            module = importlib.import_module(target)
        except ImportError as exc:
            raise StrategyError(f"cannot import strategy module {target!r}: {exc}") from exc
        source_file = getattr(module, "__file__", None)
        if source_file:
            source_path, source_sha = source_file, _sha256(Path(source_file))
    if class_name:
        cls = getattr(module, class_name, None)
        if not (inspect.isclass(cls) and issubclass(cls, Strategy)):
            raise StrategyError(f"{class_name!r} in {target} is not a Strategy subclass.")
    else:
        classes = _strategy_classes(module)
        if len(classes) != 1:
            raise StrategyError(
                f"{target} defines {len(classes)} Strategy classes; use 'file.py:ClassName'.")
        cls = classes[0]
    cls.validate_class()
    return LoadedStrategy(spec=spec, cls=cls, source_path=source_path, source_sha256=source_sha)


def _split_spec(spec: str) -> tuple[str, str]:
    """Split 'target[:ClassName]'; a Windows drive colon ('C:\\x.py') is not a class separator."""
    head, sep, tail = spec.rpartition(":")
    if not sep or not tail or any(ch in tail for ch in "\\/.") or len(head) <= 1:
        return spec, ""
    return head, tail


def list_strategies(folder: str | Path) -> list[dict[str, Any]]:
    """Every loadable Strategy in a folder (one entry per class), with load errors reported."""
    out: list[dict[str, Any]] = []
    for path in sorted(Path(folder).glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            module = _load_file(path)
            for cls in _strategy_classes(module):
                try:
                    cls.validate_class()
                    out.append({"spec": f"{path}:{cls.__name__}", "name": cls.name, "version": cls.version,
                                "timeframe": cls.timeframe, "warmup_bars": cls.warmup_bars, "ok": True})
                except StrategyError as exc:
                    out.append({"spec": f"{path}:{cls.__name__}", "ok": False, "error": str(exc)})
        except StrategyError as exc:
            out.append({"spec": str(path), "ok": False, "error": str(exc)})
    return out
