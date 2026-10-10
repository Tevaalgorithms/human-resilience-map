from __future__ import annotations

import time
from dataclasses import dataclass

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from resilience_map.agents.graph import Deps, build_graph
from resilience_map.agents.llm import LLMResponse, StubLLM
from resilience_map.core.config import Settings


class Loader:
    def __init__(self, bundle):
        self.bundle = bundle

    def load_bundle(self, _):
        return self.bundle


@dataclass
class ScriptedLLM:
    """Wraps the stub but lets a test override one prompt's output."""

    explain: str | None = None
    interpret: str | None = None
    calls: int = 0

    def complete(self, prompt, max_tokens):
        self.calls += 1
        override = {"explain_results": self.explain, "interpret_request": self.interpret}[
            prompt.name
        ]
        if override is not None:
            return LLMResponse(override, 10, 10)
        return StubLLM().complete(prompt, max_tokens)


def run(
    bundle,
    question,
    explicit=None,
    llm=None,
    settings=None,
    thread="t1",
    saver=None,
    comparison=None,
    uncertainty=None,
):
    s = settings or Settings(_env_file=None)
    saver = saver or MemorySaver()
    g = build_graph(Deps(llm=llm or StubLLM(), datasets=Loader(bundle), settings=s), saver)
    cfg = {"configurable": {"thread_id": thread}}
    out = g.invoke(
        {
            "job_id": "j",
            "question": question,
            "study_area_id": "synthetic-metro",
            "dataset_version_id": "dv",
            "explicit": explicit or {},
            "comparison_request": comparison,
            "uncertainty_request": uncertainty,
            "started_at": time.time(),
            "usage": {
                "llm_calls": 0,
                "tokens": 0,
                "tool_calls": 0,
                "analysis_runs": 0,
                "solver_seconds": 0.0,
            },
            "prompts": [],
            "clarify_round": 0,
        },
        cfg,
    )
    return g, cfg, out


Q = "Where should we open two cooling centers? driving 12 minutes"


def test_happy_path_site_selection(bundle):
    _, _, out = run(bundle, Q)
    r = out["result"]
    assert r["outcome"] == "completed" and r["explanation_source"] == "deterministic"
    assert r["verification"]["passed"] and r["narrative_check"]["ok"]
    assert r["workflow"]["prompts"] == ["interpret_request@v2", "explain_results@v2"]
    assert r["workflow"]["usage"]["tool_calls"] == 2


def test_scenario_comparison_is_deterministic_and_records_partial_failure(bundle):
    request = {
        "objective": "maximize_newly_served",
        "alternatives": [
            {"scenario_id": "walk", "constraints": {"travel_mode": "walk"}},
            {
                "scenario_id": "bad-subset",
                "constraints": {"candidate_site_ids": ["DOES-NOT-EXIST"]},
            },
        ],
    }
    _, _, first = run(bundle, Q, comparison=request, thread="compare-1")
    _, _, second = run(bundle, Q, comparison=request, thread="compare-2")
    left = first["result"]["comparison"]
    right = second["result"]["comparison"]
    assert [row["status"] for row in left["scenarios"]] == [
        "completed",
        "completed",
        "blocked",
    ]
    assert left["scenarios"][1]["coverage"] == right["scenarios"][1]["coverage"]
    assert left["scenarios"][1]["verification"]["passed"]
    assert "coverage" not in left["scenarios"][2]


def test_uncertainty_variations_are_verified_and_bounded(bundle):
    request = {
        "variants": [
            {"variant_id": "slow", "routing_speed_multiplier": 0.8},
            {"variant_id": "capacity", "candidate_capacity_multiplier": 1.2},
            {
                "variant_id": "heat-weight",
                "planning_index_weight_multipliers": {"surface_temp_anomaly_c": 1.2},
            },
        ]
    }
    _, _, out = run(bundle, Q, uncertainty=request)
    sensitivity = out["result"]["uncertainty"]
    assert len(sensitivity["runs"]) == 4
    assert all(run["verification"]["passed"] for run in sensitivity["runs"])
    assert sensitivity["coverage_range"] is not None
    assert sensitivity["site_selection_frequency"]
    assert "confidence intervals" in sensitivity["limitations"][0]


def test_missing_constraints_interrupt_then_resume(bundle):
    g, cfg, out = run(
        bundle,
        "Where should we open two cooling centers?",
        comparison={
            "alternatives": [{"scenario_id": "one-site", "constraints": {"num_centers": 1}}]
        },
    )
    intr = out["__interrupt__"][0].value
    assert len(intr["needs"]) == 2
    out2 = g.invoke(Command(resume={"travel_mode": "drive", "threshold_minutes": 10}), cfg)
    assert out2["result"]["outcome"] == "completed"
    assert out2["result"]["workflow"]["clarification_rounds"] == 1
    assert out2["result"]["comparison"]["scenarios"][1]["verification"]["passed"]


def test_invalid_value_requests_clarification(bundle):
    _, _, out = run(bundle, "Where should we open five cooling centers? walking 20 minutes")
    assert "__interrupt__" in out  # num_centers=5 violates the 1-3 limit


def test_explicit_constraints_override_question(bundle):
    _, _, out = run(bundle, Q, explicit={"num_centers": 1})
    assert out["result"]["analysis"]["constraints"]["num_centers"] == 1


def test_unsupported_question_is_incomplete(bundle):
    _, _, out = run(bundle, "Will there be a flood next week?")
    assert out["result"]["outcome"] == "incomplete" and out["result"]["analysis"] is None


def test_hallucinated_narrative_falls_back_to_deterministic(bundle):
    llm = ScriptedLLM(explain="Opening these sites will save 987654 lives, especially at SITE-77.")
    _, _, out = run(bundle, Q, llm=llm)
    r = out["result"]
    assert r["explanation_source"] == "deterministic" and not r["narrative_check"]["ok"]
    assert "987654" not in r["explanation"]


def test_invalid_llm_json_after_retries_is_incomplete(bundle):
    _, _, out = run(bundle, Q, llm=ScriptedLLM(interpret="not json"))
    assert out["result"]["outcome"] == "incomplete"


def test_token_budget_exhaustion_uses_deterministic_summary(bundle):
    s = Settings(
        _env_file=None, llm_max_tokens_per_job=1
    )  # first call spends it; explain is blocked
    _, _, out = run(bundle, Q, settings=s)
    assert out["result"]["explanation_source"] == "deterministic"
    assert out["result"]["outcome"] == "completed"


def test_wall_clock_limit(bundle):
    s = Settings(_env_file=None, job_timeout_seconds=-1)
    _, _, out = run(bundle, Q, settings=s)
    assert (
        out["result"]["outcome"] == "incomplete" and "runtime" in out["result"]["incomplete_reason"]
    )


def test_tract_explanation_and_ranking(bundle):
    tid = bundle.tracts[7].tract_id
    _, _, out = run(bundle, f"Why does tract {tid} have its score?")
    assert (
        out["result"]["analysis"]["tract"]["tract_id"] == tid
        and out["result"]["narrative_check"]["ok"]
    )
    _, _, out = run(bundle, "Which are the most vulnerable tracts? top 3")
    assert len(out["result"]["analysis"]["ranking"]) == 3


def test_unknown_tract_is_incomplete(bundle):
    _, _, out = run(bundle, "Why does tract 12345678901 have this score?")
    assert out["result"]["outcome"] == "incomplete"


def test_natural_language_network_request_never_falls_back(bundle):
    _, _, out = run(
        bundle,
        "Where should we open one cooling center, walking 20 minutes using road network routing?",
    )
    result = out.get("result") or {}
    assert result.get("outcome") != "completed"
    assert out.get("constraints", {}).get("access_model") == "road_network"


def test_live_ranking_narrative_can_reference_verified_result_count(bundle):
    _, _, out = run(
        bundle,
        "Which are the most vulnerable tracts? top 3",
        llm=ScriptedLLM(
            explain="These 3 tracts have the lowest resilience scores in the supplied ranking."
        ),
    )
    result = out["result"]
    assert result["analysis"]["returned_tract_count"] == 3
    assert result["verification"]["passed"]
    assert result["narrative_check"]["ok"]
    assert result["explanation"].startswith("These 3 tracts")
