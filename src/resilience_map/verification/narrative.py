"""Grounding check: every number and identifier in a narrative must come from the evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_NUM = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")
_LIST_MARKER = re.compile(r"(?m)^\s*\d+[.)]\s+")
_SCALE_CONSTANTS = {0.0, 100.0}  # the fixed 0-100 index scale ("45.5/100"), not a claim about data
_ID = re.compile(r"\b(?:SITE|CTR|TRACT)-[A-Za-z0-9]+\b|\b\d{11}\b|\bT\d{3,}\b")


@dataclass
class NarrativeCheck:
    ok: bool
    unsupported_numbers: list[str] = field(default_factory=list)
    unsupported_ids: list[str] = field(default_factory=list)


def _flatten(obj: Any, nums: set[float], strs: set[str]) -> None:
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        nums.add(float(obj))
    elif isinstance(obj, str):
        strs.add(obj)
        nums.update(float(m.replace(",", "")) for m in _NUM.findall(obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            strs.add(str(k))
            _flatten(v, nums, strs)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            _flatten(v, nums, strs)


def _matches(value: float, evidence: set[float]) -> bool:
    for e in evidence:
        for digits in (0, 1, 2):
            if round(e, digits) == round(value, digits) and abs(e - value) < 10 ** (-digits):
                return True
        if e != 0 and abs(e - value) / abs(e) < 0.005:
            return True
    return False


def check_narrative(narrative: str, evidence: dict[str, Any]) -> NarrativeCheck:
    nums: set[float] = set()
    strs: set[str] = set()
    _flatten(evidence, nums, strs)
    bad_nums: list[str] = []
    for tok in _NUM.findall(_LIST_MARKER.sub("", narrative)):
        value = float(tok.replace(",", ""))
        if value in _SCALE_CONSTANTS:
            continue
        if not _matches(value, nums) and tok not in bad_nums:
            bad_nums.append(tok)
    known_text = " ".join(strs)
    bad_ids = [i for i in dict.fromkeys(_ID.findall(narrative)) if i not in known_text]
    return NarrativeCheck(not bad_nums and not bad_ids, bad_nums, bad_ids)
