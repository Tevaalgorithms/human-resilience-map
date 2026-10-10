# Prompts

Prompts are versioned assets, kept out of Python code.

    prompts/<prompt_name>/<version>/system.md   # role, rules, output contract
    prompts/<prompt_name>/<version>/user.md     # Jinja2 template (StrictUndefined)

Rules:
- Never edit a released version in place; add `v2/` and bump the default in
  `agents/prompts.py::ACTIVE_VERSIONS`. The rendered prompt name+version is stored in every job
  result for traceability.
- Prompts receive only structured data (question text, tool outputs, source references).
  The model never computes scores, populations, routes or site choices.
- `tests/unit/test_prompts_and_llm.py` and `tests/unit/test_agent_roles.py` check prompt
  rendering and active versions so broken templates fail CI.

Area assistant prompts: `area_question_planner/v1` (strict routing JSON),
`data_quality_advisor/v1` (readiness advice), and `scenario_advisor/v1` (planning hypotheses).
The registry records active versions; older versions remain available for reproducibility.
