"""Compatibility shim.

Canonical implementation: scripts/telegram_notifier.py

ai_review 配下の既存 import（from telegram_notifier import ...）を壊さず、
本体を scripts 側へ一本化する。
"""
from __future__ import annotations

import sys
from importlib import util
from pathlib import Path

_CANONICAL = Path(__file__).resolve().parent.parent / "scripts" / "telegram_notifier.py"
_spec = util.spec_from_file_location(__name__, _CANONICAL)
if _spec is None or _spec.loader is None:
    raise ImportError(f"Cannot load canonical telegram_notifier: {_CANONICAL}")

_module = util.module_from_spec(_spec)
# exec 前に登録し、本体の自己参照・再importでもこの module に統一する
sys.modules[__name__] = _module
_spec.loader.exec_module(_module)
