"""LLM clients. ``BedrockLLM`` for AWS; ``StubLLM`` is a deterministic offline stand-in so the
full workflow runs locally and in tests without credentials."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from pydantic import BaseModel

from resilience_map.agents.area_plan import AreaPlan
from resilience_map.agents.plan import RequestPlan
from resilience_map.agents.prompts import RenderedPrompt
from resilience_map.core.config import Settings, get_settings
from resilience_map.core.errors import ValidationFailed


@dataclass(frozen=True)
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int
    model: str | None = None
    response_id: str | None = None


class LLMClient(Protocol):
    def complete(self, prompt: RenderedPrompt, max_tokens: int) -> LLMResponse: ...


class OpenAILLM:
    """OpenAI Responses API, strict interpretation schema and evidence-only explanations.

    No implicit retries or provider fallback; the graph owns retry and token limits.
    Credentials stay server-side and provider error bodies never reach logs/results.
    """

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        if not settings.llm_configured:
            raise ValidationFailed(
                "OpenAI is not configured. Set OPENAI_API_KEY and HRM_OPENAI_MODEL on the server, then restart the API and worker."
            )
        self._settings = settings
        self._client = client or httpx.Client(timeout=settings.llm_timeout_seconds)

    def complete(self, prompt: RenderedPrompt, max_tokens: int) -> LLMResponse:
        body: dict[str, Any] = {
            "model": self._settings.openai_model,
            "instructions": prompt.system,
            "input": [{"role": "user", "content": prompt.user}],
            "max_output_tokens": max_tokens,
            "reasoning": {"effort": self._settings.openai_reasoning_effort},
            "store": False,
        }
        schemas: dict[str, type[BaseModel]] = {
            "interpret_request": RequestPlan,
            "area_question_planner": AreaPlan,
        }
        schema_model = schemas.get(prompt.name)
        if schema_model is not None:
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": prompt.name,
                    "strict": True,
                    "schema": schema_model.model_json_schema(),
                }
            }
        try:
            with self._client.stream(
                "POST",
                "https://api.openai.com/v1/responses",
                json=body,
                headers={
                    "Authorization": "Bearer " + self._settings.openai_api_key.get_secret_value()
                },
            ) as response:
                if response.status_code >= 400:
                    messages = {
                        401: "OpenAI rejected the API key. Check the server's OPENAI_API_KEY.",
                        403: "OpenAI access denied. Check project permissions and model access.",
                        404: "OpenAI model unavailable. Check HRM_OPENAI_MODEL and account access.",
                        429: "OpenAI rate or usage limit reached. Check project limits and billing.",
                    }
                    raise ValidationFailed(
                        messages.get(
                            response.status_code,
                            f"OpenAI request failed (HTTP {response.status_code}). Retry or check model configuration.",
                        )
                    )
                data = bytearray()
                for chunk in response.iter_bytes():
                    if len(data) + len(chunk) > 500_000:
                        raise ValidationFailed(
                            "OpenAI response exceeds the application's response limit."
                        )
                    data.extend(chunk)
                payload = json.loads(data)
        except httpx.HTTPError:
            raise ValidationFailed(
                "OpenAI could not be reached within the request timeout. Retry shortly."
            ) from None
        except (ValueError, TypeError):
            raise ValidationFailed("OpenAI returned an invalid response.") from None
        try:
            if payload.get("status") != "completed":
                raise ValidationFailed(
                    "OpenAI did not complete the response. Check the output-token limit and reasoning setting."
                )
            blocks = [
                block
                for item in payload.get("output", [])
                if item.get("type") == "message"
                for block in item.get("content", [])
            ]
            if any(block.get("type") == "refusal" for block in blocks):
                raise ValidationFailed(
                    "The model declined this request. Rephrase it as a supported heat-planning question."
                )
            text = "".join(block["text"] for block in blocks if block.get("type") == "output_text")
            if not text.strip():
                raise ValidationFailed("OpenAI returned no usable text.")
            if schema_model is not None:
                text = schema_model.model_validate_json(text).model_dump_json()
            usage = payload["usage"]
            return LLMResponse(
                text,
                int(usage["input_tokens"]),
                int(usage["output_tokens"]),
                payload.get("model", self._settings.openai_model),
                payload.get("id"),
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ValidationFailed("OpenAI returned an invalid structured response.") from None


class BedrockLLM:
    def __init__(self, model_id: str, region: str) -> None:
        import boto3

        if not model_id:
            raise ValueError("HRM_BEDROCK_MODEL_ID must be set when HRM_LLM_PROVIDER=bedrock")
        self._model_id = model_id
        self._client = boto3.client("bedrock-runtime", region_name=region)

    def complete(self, prompt: RenderedPrompt, max_tokens: int) -> LLMResponse:
        resp = self._client.converse(
            modelId=self._model_id,
            system=[{"text": prompt.system}],
            messages=[{"role": "user", "content": [{"text": prompt.user}]}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": 0.0},
        )
        text = "".join(b.get("text", "") for b in resp["output"]["message"]["content"])
        usage = resp.get("usage", {})
        return LLMResponse(
            text, usage.get("inputTokens", 0), usage.get("outputTokens", 0), self._model_id
        )


_WORDS = {"one": 1, "two": 2, "three": 3, "a": 1, "single": 1}


class StubLLM:
    """Rule-based interpreter and template explainer. Behaves like the real contract:
    JSON out of ``interpret_request``, evidence-only prose out of ``explain_results``."""

    def complete(self, prompt: RenderedPrompt, max_tokens: int) -> LLMResponse:
        text = (
            self._interpret(prompt) if prompt.name == "interpret_request" else self._explain(prompt)
        )
        return LLMResponse(text, len(prompt.user) // 4, len(text) // 4)

    @staticmethod
    def _question(prompt: RenderedPrompt) -> str:
        m = re.search(r"<question>\s*(.*?)\s*</question>", prompt.user, re.S)
        return m.group(1) if m else ""

    def _interpret(self, prompt: RenderedPrompt) -> str:
        q = self._question(prompt).lower()
        out: dict[str, Any] = {
            "intent": "unsupported",
            "num_centers": None,
            "travel_mode": None,
            "access_model": None,
            "threshold_minutes": None,
            "budget_usd": None,
            "tract_id": None,
            "top_n": None,
            "reason_unsupported": None,
        }
        tract = re.search(r"\b(?:tract\s+)?(T\d{3,}|\d{11})\b", self._question(prompt), re.I)
        if re.search(r"flood|wildfire|hurricane|earthquake|air quality|forecast|will .* die", q):
            out["reason_unsupported"] = "Only historical extreme-heat planning is supported."
        elif re.search(r"cooling cent|new site|where should we (open|place|build)", q):
            out["intent"] = "site_selection"
            n = re.search(r"\b(\d+|one|two|three|a single)\b\s+(?:new\s+)?cooling", q)
            if n:
                tok = n.group(1)
                out["num_centers"] = int(tok) if tok.isdigit() else _WORDS.get(tok.split()[-1])
            if "walk" in q:
                out["travel_mode"] = "walk"
            elif "bike" in q or "cycl" in q:
                out["travel_mode"] = "bike"
            elif re.search(r"driv|\bcar\b", q):
                out["travel_mode"] = "drive"
            if re.search(r"road.?network|network rout|road rout|routed travel|using roads", q):
                out["access_model"] = "road_network"
            elif re.search(r"straight.line|proxy", q):
                out["access_model"] = "straight_line_proxy"
            mins = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|\s)?\s*min", q)
            if mins:
                out["threshold_minutes"] = float(mins.group(1))
            bud = re.search(r"\$\s?([\d,]+(?:\.\d+)?)\s*(k|m)?", q)
            if bud:
                mult = {"k": 1e3, "m": 1e6}.get(bud.group(2) or "", 1)
                out["budget_usd"] = float(bud.group(1).replace(",", "")) * mult
        elif tract and re.search(r"why|explain|score|tract", q):
            out["intent"] = "tract_explanation"
            out["tract_id"] = tract.group(1).upper()
        elif re.search(r"vulnerab|most at risk|hottest|worst", q):
            out["intent"] = "vulnerability_ranking"
            top = re.search(r"top\s+(\d+)", q)
            out["top_n"] = int(top.group(1)) if top else None
        else:
            out["reason_unsupported"] = "Question does not match a supported analysis."
        return json.dumps(out)

    def _explain(self, prompt: RenderedPrompt) -> str:
        ev = json.loads(
            prompt.user.split(
                "EVIDENCE (verified tool outputs; the only allowed source of facts):", 1
            )[1].strip()
        )
        # Templated from evidence only, so the narrative check passes by construction.
        from resilience_map.verification.summary import summarize

        return summarize(ev)


def get_llm(settings: Settings | None = None) -> LLMClient:
    s = settings or get_settings()
    if s.llm_provider == "openai":
        return OpenAILLM(s)
    if s.llm_provider == "bedrock":
        return BedrockLLM(s.bedrock_model_id, s.bedrock_region)
    if s.llm_provider == "stub":
        return StubLLM()
    raise ValueError("Unsupported LLM provider")
