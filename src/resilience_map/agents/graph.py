"""Checkpointed planning workflow with deterministic readiness and analytical roles.

Only ``interpret`` and ``explain`` call an LLM. Retrieval, analysis and verification are ordinary
deterministic Python. Scenario comparison and uncertainty assessment execute the same approved
analysis and independent verification paths sequentially. Clarification uses LangGraph ``interrupt``
so a ``needs_input`` job resumes from its persisted checkpoint.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt
from pydantic import ValidationError

from resilience_map.agents import prompts
from resilience_map.agents.comparison import comparison_result, scenario_summary
from resilience_map.agents.llm import LLMClient
from resilience_map.agents.readiness import FreshnessPolicy
from resilience_map.agents.state import Usage, WorkflowState
from resilience_map.agents.tools import INTENT_TOOL, Toolbox
from resilience_map.agents.uncertainty import sensitivity_summary, uncertainty_result
from resilience_map.core.config import Settings
from resilience_map.core.errors import LimitExceeded, ValidationFailed
from resilience_map.core.models import AnalysisOverrides, DatasetBundle, ScenarioConstraints
from resilience_map.verification.checks import verify_scores, verify_site_selection
from resilience_map.verification.narrative import check_narrative
from resilience_map.verification.summary import summarize

log = logging.getLogger(__name__)

CONSTRAINT_KEYS = (
    "num_centers",
    "travel_mode",
    "threshold_minutes",
    "budget_usd",
    "candidate_site_ids",
    "access_model",
    "bbox",
)
FIELD_HELP = {
    "num_centers": "How many new cooling centers (1-3)?",
    "travel_mode": "Travel mode for access: walk, bike or drive?",
    "threshold_minutes": "Maximum travel time to a center, in minutes?",
}


class DatasetLoader(Protocol):
    def load_bundle(self, dataset_version_id: str) -> DatasetBundle: ...


@dataclass
class Deps:
    llm: LLMClient
    datasets: DatasetLoader
    settings: Settings
    progress: Callable[[str, str], None] = lambda job_id, msg: None


def _usage(state: WorkflowState) -> Usage:
    usage: dict[str, Any] = {
        "llm_calls": 0,
        "tokens": 0,
        "tool_calls": 0,
        "analysis_runs": 0,
        "solver_seconds": 0.0,
    }
    usage.update(state.get("usage") or {})
    return cast(Usage, usage)


def build_graph(deps: Deps, checkpointer: BaseCheckpointSaver[Any] | None = None) -> Any:
    s = deps.settings
    limits = {
        "solver_max_seconds": s.solver_max_seconds,
        "max_candidate_sites": s.max_candidate_sites,
        "max_tool_calls": s.max_tool_calls_per_job,
    }
    freshness_policy = FreshnessPolicy(
        source_max_age_days=s.source_freshness_days,
        inventory_max_age_days=s.inventory_freshness_days,
        road_max_age_days=s.road_freshness_days,
    )

    def toolbox_call(
        bundle: DatasetBundle,
        used: Usage,
        name: str,
        arguments: dict[str, Any],
        solver_seconds: float | None = None,
    ) -> dict[str, Any]:
        call_limits = dict(limits)
        if solver_seconds is not None:
            call_limits["solver_max_seconds"] = solver_seconds
        box = Toolbox(bundle, call_limits, used["tool_calls"])
        result = box.call(name, arguments)
        used["tool_calls"] = box.calls
        return result

    def readiness_for(
        bundle: DatasetBundle,
        used: Usage,
        operation: str,
        constraints: ScenarioConstraints | None,
    ) -> dict[str, Any]:
        return toolbox_call(
            bundle,
            used,
            "assess_data_readiness",
            {
                "operation": operation,
                "constraints": constraints.model_dump() if constraints else None,
                "freshness_policy": freshness_policy.model_dump(),
            },
        )

    def execute_site_run(
        state: WorkflowState,
        bundle: DatasetBundle,
        used: Usage,
        constraints: ScenarioConstraints,
        overrides: AnalysisOverrides | None = None,
    ) -> dict[str, Any]:
        if used["analysis_runs"] >= s.max_analysis_runs_per_job:
            raise LimitExceeded("Analysis-run budget exhausted")
        remaining = s.solver_total_seconds_per_job - used["solver_seconds"]
        if remaining <= 0:
            raise LimitExceeded("Total solver-time budget exhausted")
        if time.time() - state["started_at"] > s.job_timeout_seconds:
            raise LimitExceeded("Job exceeded its maximum runtime")
        analysis = toolbox_call(
            bundle,
            used,
            "select_cooling_sites",
            {
                "constraints": constraints.model_dump(),
                "overrides": (overrides or AnalysisOverrides()).model_dump(),
            },
            min(s.solver_max_seconds, remaining),
        )
        used["analysis_runs"] += 1
        used["solver_seconds"] += float(analysis.get("solver", {}).get("runtime_seconds", 0))
        return analysis

    def guarded(name: str, fn: Callable[[WorkflowState], dict[str, Any]]) -> Callable[..., Any]:
        """Wrap a node: enforce the wall-clock limit, convert expected failures to 'incomplete'."""

        def run(state: WorkflowState) -> dict[str, Any]:
            try:
                if time.time() - state["started_at"] > s.job_timeout_seconds:
                    raise LimitExceeded("Job exceeded its maximum runtime")
                deps.progress(state["job_id"], name)
                return fn(state)
            except (LimitExceeded, ValidationFailed) as e:
                log.warning("node %s ended workflow: %s", name, e)
                return {"status": "incomplete", "incomplete_reason": str(e)}

        return run

    def call_llm(
        state: WorkflowState, prompt: prompts.RenderedPrompt
    ) -> tuple[str, dict[str, Any]]:
        u = _usage(state)
        if u["llm_calls"] >= s.llm_max_calls_per_job or u["tokens"] >= s.llm_max_tokens_per_job:
            raise LimitExceeded("LLM call/token budget exhausted")
        resp = deps.llm.complete(prompt, s.llm_max_output_tokens)
        u["llm_calls"] += 1
        u["tokens"] += resp.input_tokens + resp.output_tokens
        return resp.text, {
            "usage": u,
            "prompts": [*state.get("prompts", []), prompt.ref],
            "model_calls": [
                *state.get("model_calls", []),
                {
                    "prompt": prompt.ref,
                    "provider": s.llm_provider,
                    "model": resp.model or s.llm_model,
                    "response_id": resp.response_id,
                    "input_tokens": resp.input_tokens,
                    "output_tokens": resp.output_tokens,
                },
            ],
        }

    # ---- nodes -------------------------------------------------------------------------
    def interpret(state: WorkflowState) -> dict[str, Any]:
        prompt = prompts.render(
            "interpret_request",
            study_area_id=state["study_area_id"],
            known_constraints_json=json.dumps(state.get("explicit", {}), sort_keys=True),
            question=state["question"],
        )
        update: dict[str, Any] = {}
        for attempt in range(s.llm_max_retries + 1):
            text, update = call_llm(cast(WorkflowState, {**state, **update}), prompt)
            try:
                plan = json.loads(text[text.index("{") : text.rindex("}") + 1])
                if plan.get("intent") not in {*INTENT_TOOL, "unsupported"}:
                    raise ValueError("bad intent")
                return {**update, "plan": plan, "intent": plan["intent"]}
            except ValueError:
                log.warning("interpret returned invalid JSON (attempt %d)", attempt + 1)
        return {
            **update,
            "status": "incomplete",
            "incomplete_reason": "Could not interpret the request into a valid plan.",
        }

    def validate_plan(state: WorkflowState) -> dict[str, Any]:
        plan, explicit = state.get("plan", {}), state.get("explicit", {})
        if state["intent"] == "unsupported":
            return {
                "status": "incomplete",
                "incomplete_reason": plan.get("reason_unsupported")
                or "This question is not supported. Supported: cooling-center siting, "
                "tract explanations and vulnerability rankings for extreme heat.",
            }
        merged = {k: explicit.get(k, plan.get(k)) for k in (*CONSTRAINT_KEYS, "tract_id", "top_n")}
        needs: list[str] = []
        if state["intent"] == "site_selection":
            missing = [k for k in FIELD_HELP if merged.get(k) is None]
            needs += [FIELD_HELP[k] for k in missing]
            if not missing:
                try:
                    ScenarioConstraints.model_validate(
                        {
                            k: merged.get(k)
                            for k in CONSTRAINT_KEYS
                            if k != "access_model" or merged.get(k) is not None
                        }
                    )
                except ValidationError as e:
                    needs += [
                        f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()
                    ]
        elif state["intent"] == "tract_explanation" and not merged.get("tract_id"):
            needs.append("Which tract id should be explained?")
        return {"constraints": merged, "needs": needs}

    def clarify(state: WorkflowState) -> dict[str, Any]:
        rounds = state.get("clarify_round", 0)
        if rounds >= 3:
            raise ValidationFailed("Required inputs are still missing after 3 clarification rounds")
        answer = interrupt(
            {"message": "More information is needed to run this analysis.", "needs": state["needs"]}
        )
        explicit = {
            **state.get("explicit", {}),
            **{k: v for k, v in dict(answer).items() if v is not None and v != ""},
        }
        return {"explicit": explicit, "clarify_round": rounds + 1}

    def retrieve(state: WorkflowState) -> dict[str, Any]:
        deps.datasets.load_bundle(state["dataset_version_id"])  # fail fast on unpublished/missing
        return {}

    def assess_data_readiness(state: WorkflowState) -> dict[str, Any]:
        bundle = deps.datasets.load_bundle(state["dataset_version_id"])
        used = _usage(state)
        constraints = None
        if state["intent"] == "site_selection":
            constraints = ScenarioConstraints.model_validate(
                {
                    k: state["constraints"].get(k)
                    for k in CONSTRAINT_KEYS
                    if state["constraints"].get(k) is not None
                }
            )
        readiness = readiness_for(bundle, used, state["intent"], constraints)
        result: dict[str, Any] = {"readiness": readiness, "usage": used}
        if readiness["status"] == "blocked":
            result.update(
                status="incomplete",
                incomplete_reason="Data readiness blocked this operation: "
                + "; ".join(readiness["missing_inputs"][:3]),
            )
        return result

    def analyze(state: WorkflowState) -> dict[str, Any]:
        bundle = deps.datasets.load_bundle(state["dataset_version_id"])
        used = _usage(state)
        tool = INTENT_TOOL[state["intent"]]
        c = state["constraints"]
        args: dict[str, Any] = {
            "select_cooling_sites": lambda: {
                "constraints": {
                    k: c.get(k)
                    for k in CONSTRAINT_KEYS
                    if k != "access_model" or c.get(k) is not None
                }
            },
            "explain_tract": lambda: {"tract_id": c["tract_id"]},
            "rank_vulnerable_tracts": lambda: {"top_n": c.get("top_n") or 5},
        }[tool]()
        if tool == "select_cooling_sites":
            constraints = ScenarioConstraints.model_validate(args["constraints"])
            analysis = execute_site_run(state, bundle, used, constraints)
        else:
            analysis = toolbox_call(bundle, used, tool, args)
        return {"analysis": analysis, "usage": used}

    def verify(state: WorkflowState) -> dict[str, Any]:
        a = state["analysis"]
        if a["intent"] == "site_selection":
            bundle = deps.datasets.load_bundle(state["dataset_version_id"])
            report = verify_site_selection(a, bundle)
        elif a["intent"] == "tract_explanation":
            report = verify_scores({a["tract"]["tract_id"]: a["tract"]["score"]})
        else:
            report = verify_scores({r["tract_id"]: r["score"] for r in a["ranking"]})
            report.check(
                a["returned_tract_count"] == len(a["ranking"]),
                "Returned tract count does not match ranking rows",
            )
        out: dict[str, Any] = {"verification": report.to_dict()}
        if not report.passed:
            out |= {
                "status": "incomplete",
                "incomplete_reason": "Verification failed: " + "; ".join(report.errors[:3]),
            }
        return out

    def compare_scenarios(state: WorkflowState) -> dict[str, Any]:
        request = state.get("comparison_request")
        if not request:
            return {"comparison": None}
        if state["intent"] != "site_selection":
            rows = [
                scenario_summary(
                    alternative["scenario_id"],
                    "invalid",
                    state["readiness"],
                    reason="Scenario comparison is available only for site-selection analyses",
                )
                for alternative in request["alternatives"]
            ]
            return {
                "comparison": comparison_result(
                    state["dataset_version_id"],
                    state["analysis"]["scoring_version"],
                    rows,
                    request.get("objective"),
                )
            }

        bundle = deps.datasets.load_bundle(state["dataset_version_id"])
        used = _usage(state)
        rows = [
            scenario_summary(
                "requested",
                "completed",
                state["readiness"],
                state["analysis"],
                state["verification"],
            )
        ]
        base = ScenarioConstraints.model_validate(
            {
                k: state["constraints"].get(k)
                for k in CONSTRAINT_KEYS
                if state["constraints"].get(k) is not None
            }
        )
        alternatives = request["alternatives"][: max(0, s.max_comparison_scenarios - 1)]
        for index, alternative in enumerate(alternatives):
            scenario_id = alternative["scenario_id"]
            merged = base.model_dump()
            merged.update(
                {
                    key: value
                    for key, value in alternative.get("constraints", {}).items()
                    if key in CONSTRAINT_KEYS and value is not None
                }
            )
            try:
                constraints = ScenarioConstraints.model_validate(merged)
                readiness = readiness_for(bundle, used, "site_selection", constraints)
                if readiness["status"] == "blocked":
                    rows.append(
                        scenario_summary(
                            scenario_id,
                            "blocked",
                            readiness,
                            reason="; ".join(readiness["missing_inputs"][:3]),
                        )
                    )
                    continue
                analysis = execute_site_run(state, bundle, used, constraints)
                solver_status = analysis["solver"]["status"]
                if solver_status not in {"OPTIMAL", "FEASIBLE"}:
                    rows.append(
                        scenario_summary(
                            scenario_id,
                            "infeasible",
                            readiness,
                            analysis=analysis,
                            reason="; ".join(analysis["solver"].get("notes", [])),
                        )
                    )
                    continue
                verification = verify_site_selection(analysis, bundle).to_dict()
                status = "completed" if verification["passed"] else "failed"
                rows.append(
                    scenario_summary(
                        scenario_id,
                        status,
                        readiness,
                        analysis,
                        verification,
                        None if verification["passed"] else "; ".join(verification["errors"][:3]),
                    )
                )
            except (ValidationError, ValidationFailed, ValueError) as exc:
                rows.append(
                    scenario_summary(scenario_id, "invalid", state["readiness"], reason=str(exc))
                )
            except LimitExceeded as exc:
                rows.append(
                    scenario_summary(scenario_id, "failed", state["readiness"], reason=str(exc))
                )
                for remaining in alternatives[index + 1 :]:
                    rows.append(
                        scenario_summary(
                            remaining["scenario_id"],
                            "failed",
                            state["readiness"],
                            reason="Total job budget exhausted before this alternative ran",
                        )
                    )
                break
        return {
            "comparison": comparison_result(
                bundle.dataset_version_id,
                bundle.scoring_version,
                rows,
                request.get("objective"),
            ),
            "usage": used,
        }

    def assess_uncertainty(state: WorkflowState) -> dict[str, Any]:
        request = state.get("uncertainty_request")
        if not request:
            return {"uncertainty": None}
        if state["intent"] != "site_selection":
            return {
                "uncertainty": uncertainty_result(
                    state["dataset_version_id"],
                    state["analysis"]["scoring_version"],
                    [],
                    [
                        *request.get("unsupported_parameters", []),
                        "Sensitivity execution requires a site-selection analysis",
                    ],
                )
            }

        bundle = deps.datasets.load_bundle(state["dataset_version_id"])
        used = _usage(state)
        runs = [
            sensitivity_summary(
                "reference",
                "completed",
                state["analysis"].get("parameter_overrides", {}),
                state["analysis"],
                state["verification"],
            )
        ]
        constraints = ScenarioConstraints.model_validate(
            {
                k: state["constraints"].get(k)
                for k in CONSTRAINT_KEYS
                if state["constraints"].get(k) is not None
            }
        )
        variants = request["variants"][: max(0, s.max_sensitivity_runs - 1)]
        for index, variant in enumerate(variants):
            variant_id = variant["variant_id"]
            tested = {key: value for key, value in variant.items() if key != "variant_id"}
            try:
                overrides = AnalysisOverrides.model_validate(tested)
                analysis = execute_site_run(state, bundle, used, constraints, overrides)
                solver_status = analysis["solver"]["status"]
                if solver_status not in {"OPTIMAL", "FEASIBLE"}:
                    runs.append(
                        sensitivity_summary(
                            variant_id,
                            "infeasible",
                            tested,
                            analysis=analysis,
                            reason="; ".join(analysis["solver"].get("notes", [])),
                        )
                    )
                    continue
                verification = verify_site_selection(analysis, bundle).to_dict()
                runs.append(
                    sensitivity_summary(
                        variant_id,
                        "completed" if verification["passed"] else "failed",
                        tested,
                        analysis,
                        verification,
                        None if verification["passed"] else "; ".join(verification["errors"][:3]),
                    )
                )
            except (ValidationError, ValidationFailed, ValueError) as exc:
                runs.append(sensitivity_summary(variant_id, "invalid", tested, reason=str(exc)))
            except LimitExceeded as exc:
                runs.append(sensitivity_summary(variant_id, "failed", tested, reason=str(exc)))
                for remaining in variants[index + 1 :]:
                    runs.append(
                        sensitivity_summary(
                            remaining["variant_id"],
                            "failed",
                            {k: v for k, v in remaining.items() if k != "variant_id"},
                            reason="Total job budget exhausted before this variant ran",
                        )
                    )
                break
        return {
            "uncertainty": uncertainty_result(
                bundle.dataset_version_id,
                bundle.scoring_version,
                runs,
                request.get("unsupported_parameters", []),
            ),
            "usage": used,
        }

    def explain(state: WorkflowState) -> dict[str, Any]:
        evidence = state["analysis"]
        prompt = prompts.render(
            "explain_results",
            question=state["question"],
            evidence_json=json.dumps(evidence, sort_keys=True, default=str),
        )
        try:
            text, update = call_llm(state, prompt)
        except LimitExceeded:
            return {"explanation": summarize(evidence), "explanation_source": "deterministic"}
        return {
            **update,
            "explanation": text.strip(),
            "explanation_source": "deterministic" if s.llm_provider == "stub" else "llm",
        }

    def check_narr(state: WorkflowState) -> dict[str, Any]:
        chk = check_narrative(state["explanation"], state["analysis"])
        out: dict[str, Any] = {
            "narrative_check": {
                "ok": chk.ok,
                "unsupported_numbers": chk.unsupported_numbers,
                "unsupported_ids": chk.unsupported_ids,
            }
        }
        if not chk.ok:
            log.warning("narrative failed grounding check; using deterministic summary")
            out |= {
                "explanation": summarize(state["analysis"]),
                "explanation_source": "deterministic",
            }
        return out

    def finalize(state: WorkflowState) -> dict[str, Any]:
        done = state.get("status") != "incomplete"
        analysis = state.get("analysis") if done else None
        result = {
            "outcome": "completed" if done else "incomplete",
            "intent": state.get("intent"),
            "incomplete_reason": None if done else state.get("incomplete_reason"),
            "analysis": analysis,
            "explanation": state.get("explanation") if done else None,
            "explanation_source": state.get("explanation_source") if done else None,
            "verification": state.get("verification"),
            "readiness": state.get("readiness"),
            "comparison": state.get("comparison"),
            "uncertainty": state.get("uncertainty"),
            "narrative_check": state.get("narrative_check"),
            "workflow": {
                "llm_provider": s.llm_provider,
                "llm_model": s.llm_model,
                "model_calls": state.get("model_calls", []),
                "prompts": state.get("prompts", []),
                "usage": state.get("usage"),
                "clarification_rounds": state.get("clarify_round", 0),
                "roles": {
                    "llm": ["request_interpretation", "result_explanation"],
                    "deterministic": [
                        "data_readiness",
                        "analysis",
                        "independent_verification",
                        "scenario_comparison",
                        "uncertainty_assessment",
                    ],
                },
            },
        }
        if len(json.dumps(result, default=str).encode("utf-8")) > s.max_result_bytes:
            return {
                "result": {
                    "outcome": "incomplete",
                    "intent": state.get("intent"),
                    "incomplete_reason": "Result exceeded the configured report-size limit",
                    "readiness": state.get("readiness"),
                    "workflow": result["workflow"],
                },
                "status": "incomplete",
            }
        return {"result": result, "status": "completed" if done else "incomplete"}

    # ---- wiring ------------------------------------------------------------------------
    g: StateGraph[WorkflowState, None, WorkflowState, WorkflowState] = StateGraph(WorkflowState)
    for name, fn in [
        ("interpret", interpret),
        ("validate_plan", validate_plan),
        ("retrieve", retrieve),
        ("assess_data_readiness", assess_data_readiness),
        ("analyze", analyze),
        ("verify", verify),
        ("compare_scenarios", compare_scenarios),
        ("assess_uncertainty", assess_uncertainty),
        ("explain", explain),
        ("check_narrative", check_narr),
    ]:
        g.add_node(name, guarded(name, fn))
    g.add_node("clarify", guarded("clarify", clarify))
    g.add_node("finalize", finalize)
    g.set_entry_point("interpret")

    def after(nxt: str) -> Callable[[WorkflowState], str]:
        return lambda st: "finalize" if st.get("status") == "incomplete" else nxt

    g.add_conditional_edges("interpret", after("validate_plan"))
    g.add_conditional_edges(
        "validate_plan",
        lambda st: (
            "finalize"
            if st.get("status") == "incomplete"
            else ("clarify" if st.get("needs") else "retrieve")
        ),
    )
    g.add_conditional_edges("clarify", after("validate_plan"))
    g.add_conditional_edges("retrieve", after("assess_data_readiness"))
    g.add_conditional_edges("assess_data_readiness", after("analyze"))
    g.add_conditional_edges("analyze", after("verify"))
    g.add_conditional_edges("verify", after("compare_scenarios"))
    g.add_conditional_edges("compare_scenarios", after("assess_uncertainty"))
    g.add_conditional_edges("assess_uncertainty", after("explain"))
    g.add_edge("explain", "check_narrative")
    g.add_edge("check_narrative", "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)
