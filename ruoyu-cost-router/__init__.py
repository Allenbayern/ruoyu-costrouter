"""Private controller-first cost-router plugin.

Routes are defined by the operator in ``plugins.entries.ruoyu-cost-router.routes``.
The plugin never resolves Hermes profiles and never shells out; each dispatch uses the
host-owned ``ctx.llm`` facade, whose override trust policy is enforced before a
provider call is attempted.
"""

from __future__ import annotations

import json
from typing import Any

from agent.redact import redact_sensitive_text
from agent.plugin_llm import PluginLlmTrustError
from hermes_cli.config import load_config

_PLUGIN_ID = "ruoyu-cost-router"
_ROUTE_PAIRS = {
    "luna": ("custom", "gpt-5.6-luna"),
    "luna_economy": ("deepseek", "deepseek-v4-flash"),
    "terra": ("custom", "gpt-5.6-terra"),
    "sol": ("custom", "gpt-5.6-sol"),
}
_TIERS = tuple(_ROUTE_PAIRS)
_TASK_TYPE_TIERS = {
    "router": "luna",
    "classify": "luna",
    "dedupe": "luna_economy",
    "bulk_preprocess": "luna_economy",
    "rag_answer": "terra",
    "draft": "terra",
    "coding": "terra",
    "final_review": "sol",
    "architecture": "sol",
}
_KEYWORD_TIERS = (
    ("sol", ("final", "audit", "review", "architecture", "conflict", "security")),
    ("terra", ("draft", "rewrite", "article", "script", "coding", "implement")),
    ("luna_economy", ("bulk", "batch", "many", "economy", "low cost", "dedupe")),
    ("luna", ("router", "classify", "tag", "metadata", "filter")),
)
_MAX_PROMPT_CHARS = 24_000


def _safe_text(value: Any, limit: int | None = None) -> str:
    try:
        text = redact_sensitive_text(str(value or ""), force=True)
    except Exception:
        return "[REDACTED - redaction failed]"
    return text[:limit] if limit is not None else text


def _safe_json(payload: dict[str, Any]) -> str:
    return _safe_text(json.dumps(payload, ensure_ascii=False), limit=None)


def _error(message: str, *, tier: str | None = None) -> str:
    result: dict[str, Any] = {
        "routing_status": "denied",
        "controller_decision_required": True,
        "error": _safe_text(message, limit=2_000),
    }
    if tier:
        result["tier"] = tier
    return _safe_json(result)


def _plugin_config() -> dict[str, Any]:
    try:
        cfg = load_config() or {}
    except Exception:
        return {}
    entries = (cfg.get("plugins") or {}).get("entries") or {}
    entry = entries.get(_PLUGIN_ID) or {}
    return entry if isinstance(entry, dict) else {}


def _select_tier(
    route: str | None,
    goal: str,
    context: str | None,
    task_type: str | None,
    project: dict[str, Any] | None,
) -> tuple[str, str, str]:
    explicit = route or (project or {}).get("route") or (project or {}).get("route_candidate")
    if explicit:
        tier = str(explicit).strip().lower().replace("-", "_")
        if tier not in _TIERS:
            raise ValueError("route must be one of: luna, luna_economy, terra, sol")
        return tier, "explicit", f"route:{tier}"

    normalized_task_type = (task_type or (project or {}).get("task_type") or "").strip().lower().replace("-", "_")
    if normalized_task_type in _TASK_TYPE_TIERS:
        tier = _TASK_TYPE_TIERS[normalized_task_type]
        return tier, "task_type", f"task_type:{normalized_task_type}"

    text = f"{goal}\n{context or ''}".lower()
    for tier, keywords in _KEYWORD_TIERS:
        for keyword in keywords:
            if keyword in text:
                return tier, "keyword", f"keyword:{keyword}"
    return "terra", "default", "default:terra"


def _required_allowlist(llm: dict[str, Any], key: str, expected: str) -> None:
    values = llm.get(key)
    if not isinstance(values, list) or not values or not all(isinstance(value, str) and value.strip() for value in values):
        raise ValueError(f"host trust policy requires a non-empty {key} list")
    normalized = {value.strip().lower() for value in values}
    if "*" in normalized:
        raise ValueError(f"host trust policy {key} must not use wildcard values")
    if expected.lower() not in normalized:
        raise ValueError(f"host trust policy {key} does not allow the required exact route value")
    known_values = {
        provider.lower() if key == "allowed_providers" else model.lower()
        for provider, model in _ROUTE_PAIRS.values()
    }
    if not normalized <= known_values:
        raise ValueError(f"host trust policy {key} contains an unknown route value")


def _authorize_route(tier: str) -> tuple[str, str]:
    """Fail closed before any caller invocation for this tier's fixed pair."""
    config = _plugin_config()
    expected_provider, expected_model = _ROUTE_PAIRS[tier]
    llm = config.get("llm")
    if not isinstance(llm, dict):
        raise ValueError("operator configuration is missing host LLM trust policy")
    if llm.get("allow_provider_override") is not True:
        raise ValueError("host trust policy must explicitly allow provider overrides")
    if llm.get("allow_model_override") is not True:
        raise ValueError("host trust policy must explicitly allow model overrides")
    _required_allowlist(llm, "allowed_providers", expected_provider)
    _required_allowlist(llm, "allowed_models", expected_model)

    routes = config.get("routes")
    if not isinstance(routes, dict):
        raise ValueError("operator configuration is missing plugins.entries.ruoyu-cost-router.routes")
    route = routes.get(tier)
    if not isinstance(route, dict):
        raise ValueError(f"operator configuration is missing route {tier!r}")
    enabled = route.get("enabled", True)
    if enabled is not True:
        if enabled is False:
            raise ValueError(f"route {tier!r} is disabled by operator configuration")
        raise ValueError(f"route {tier!r} enabled flag must be true or false")
    provider = route.get("provider")
    model = route.get("model")
    if not isinstance(provider, str) or not isinstance(model, str):
        raise ValueError(f"route {tier!r} must declare its immutable provider and model")
    if (provider.strip().lower(), model.strip().lower()) != (expected_provider, expected_model):
        raise ValueError(f"route {tier!r} may not override its immutable provider/model pair")
    return expected_provider, expected_model


def _prompt(goal: str, context: str | None, tier: str, task_type: str | None) -> str:
    parts = [
        "You are a bounded worker invoked by a controller-first local routing plugin.",
        "Return evidence, a draft, or local analysis only.",
        "Do not make final publication, merge, deployment, production, configuration, blocker, risk-acceptance, or stage-promotion decisions.",
        "State unverified items explicitly.",
        f"Tier: {tier}; task_type: {task_type or 'auto'}.",
        "",
        "Goal:",
        goal.strip(),
    ]
    if context and context.strip():
        parts.extend(["", "Context:", context.strip()])
    return "\n".join(parts)[:_MAX_PROMPT_CHARS]


def _handler(ctx, args: dict[str, Any], **_: Any) -> str:
    goal = args.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        return _error("ruoyu_cost_router requires a non-empty goal")
    context = args.get("context")
    route = args.get("route")
    task_type = args.get("task_type")
    project = args.get("project")
    if context is not None and not isinstance(context, str):
        return _error("context must be a string when supplied")
    if route is not None and not isinstance(route, str):
        return _error("route must be a string when supplied")
    if task_type is not None and not isinstance(task_type, str):
        return _error("task_type must be a string when supplied")
    if project is not None and not isinstance(project, dict):
        return _error("project must be an object when supplied")

    try:
        tier, selection_mode, matched_rule = _select_tier(route, goal, context, task_type, project)
        provider, model = _authorize_route(tier)
        result = ctx.llm.complete(
            messages=[{"role": "user", "content": _prompt(goal, context, tier, task_type)}],
            provider=provider,
            model=model,
            purpose=f"{_PLUGIN_ID}.{tier}",
        )
    except PluginLlmTrustError as exc:
        return _error(str(exc), tier=locals().get("tier"))
    except ValueError as exc:
        return _error(str(exc), tier=locals().get("tier"))
    except Exception as exc:
        return _error(f"host-owned route call failed: {exc}", tier=locals().get("tier"))

    return _safe_json({
        "routing_status": "completed",
        "tier": tier,
        "route": tier,
        "selection_mode": selection_mode,
        "matched_rule": matched_rule,
        "provider": _safe_text(result.provider, limit=200),
        "model": _safe_text(result.model, limit=500),
        "output": _safe_text(result.text),
        "usage": {
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
            "total_tokens": result.usage.total_tokens,
            "cost_usd": result.usage.cost_usd,
        },
        "worker_result": {
            "status": "partial",
            "deliverable": _safe_text(result.text),
            "evidence": [],
            "unverified_items": ["Controller must independently verify routed output."],
            "controller_decisions": ["Controller retains final acceptance and all irreversible decisions."],
        },
        "controller_decision_required": True,
    })


_SCHEMA = {
    "name": "ruoyu_cost_router",
    "description": "Route one bounded request through operator-defined local plugin tiers using host-owned LLM access. The controller retains final decisions.",
    "parameters": {
        "type": "object",
        "properties": {
            "goal": {"type": "string", "description": "Self-contained bounded task and deliverable."},
            "context": {"type": "string", "description": "Optional background needed for the route."},
            "route": {"type": "string", "enum": list(_TIERS), "description": "Optional explicit operator-defined tier."},
            "task_type": {"type": "string", "description": "Optional routing category."},
            "project": {"type": "object", "description": "Optional routing metadata; only route, route_candidate, and task_type affect selection."},
        },
        "required": ["goal"],
    },
}


def register(ctx) -> None:
    ctx.register_tool(
        name="ruoyu_cost_router",
        toolset="delegation",
        schema=_SCHEMA,
        handler=lambda args, **kwargs: _handler(ctx, args, **kwargs),
        emoji="R",
    )
