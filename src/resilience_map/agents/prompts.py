"""Prompt registry: loads versioned prompt files from ``resilience_map/prompts``."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any

from jinja2 import Environment, StrictUndefined

ACTIVE_VERSIONS: dict[str, str] = {
    "area_question_planner": "v1",
    "data_quality_advisor": "v1",
    "scenario_advisor": "v1",
    "interpret_request": "v2",
    "explain_results": "v2",
}

_env = Environment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=False)  # noqa: S701


@dataclass(frozen=True)
class RenderedPrompt:
    name: str
    version: str
    system: str
    user: str

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}"


@lru_cache
def _load(name: str, version: str, part: str) -> str:
    path = resources.files("resilience_map.prompts") / name / version / f"{part}.md"
    return path.read_text(encoding="utf-8").strip()


def render(name: str, version: str | None = None, **variables: Any) -> RenderedPrompt:
    v = version or ACTIVE_VERSIONS[name]
    return RenderedPrompt(
        name=name,
        version=v,
        system=_env.from_string(_load(name, v, "system")).render(**variables),
        user=_env.from_string(_load(name, v, "user")).render(**variables),
    )
