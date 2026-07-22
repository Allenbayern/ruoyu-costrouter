"""Private controller-first, budget-aware routing plugin.

The operator-owned route catalog is validated before every dispatch. The plugin
uses the host-owned ``ctx.llm`` facade and never resolves profiles or shells out.
"""

from __future__ import annotations

import json
from typing import Any

from agent.redact import redact_sensitive_text
from hermes_cli.config import load_config

_PLUGIN_ID = "ruoyu-cost-router"
_CATALOG_VERSION = 2
_TIERS = ("luna", "luna_economy", "terra", "sol")
_WORKER_PROFILES = {
    "luna": "worker-luna",
    "luna_economy": "worker-luna-economy",
    "terra": "worker-terra",
    "sol": "worker-sol",
}
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
_DEFAULT_FALLBACKS = {
    "luna": (),
    "luna_economy": ("luna",),
    "terra": ("luna_economy", "luna"),
    "sol": ("terra", "luna_economy", "luna"),
}
_MAX_GOAL_CHARS = 12_000
_MAX_CONTEXT_CHARS = 12_000
_MAX_OUTPUT_CHARS = 12_000
_MAX_TASK_BODY_BYTES = 8 * 1024  # Hermes Kanban worker-context contract.
_CHARS_PER_TOKEN = 4


def _safe_text(value: Any, limit: int | None = None) -> str:
    try:
        text = redact_sensitive_text(str(value or ""), force=True)
    except Exception:
        return "[REDACTED - redaction failed]"
    return text[:limit] if limit is not None else text


def _safe_json(payload: dict[str, Any]) -> str:
    return _safe_text(json.dumps(payload, ensure_ascii=False), limit=None)


def _error(message: str, *, tier: str | None = None, budget: dict[str, Any] | None = None) -> str:
    result: dict[str, Any] = {
        "routing_status": "denied",
        "controller_decision_required": True,
        "error": _safe_text(message, limit=2_000),
    }
    if tier:
        result["tier"] = tier
    if budget:
        result["budget"] = budget
    return _safe_json(result)


def _plugin_config() -> dict[str, Any]:
    try:
        cfg = load_config() or {}
    except Exception:
        return {}
    entries = (cfg.get("plugins") or {}).get("entries") or {}
    entry = entries.get(_PLUGIN_ID) or {}
    return entry if isinstance(entry, dict) else {}


def _bounded_text(value: str, limit: int) -> tuple[str, bool]:
    return value[:limit], len(value) > limit


def _selection_text(goal: str, context: str | None) -> str:
    return f"{goal}\n{context or ''}".lower()


def _select_tier(
    route: str | None,
    goal: str,
    context: str | None,
    task_type: str | None,
    project: dict[str, Any] | None,
    routing: dict[str, Any],
) -> tuple[str, str, str]:
    explicit = route or (project or {}).get("route") or (project or {}).get("route_candidate")
    if explicit:
        tier = str(explicit).strip().lower().replace("-", "_")
        if tier not in _TIERS:
            raise ValueError("route must be one of: luna, luna_economy, terra, sol")
        return tier, "explicit", f"route:{tier}"

    candidate = task_type or (project or {}).get("task_type")
    if candidate is not None:
        normalized = str(candidate).strip().lower().replace("-", "_")
        if normalized not in _TASK_TYPE_TIERS:
            allowed = ", ".join(sorted(_TASK_TYPE_TIERS))
            raise ValueError(f"task_type must be one of: {allowed}")
        return _TASK_TYPE_TIERS[normalized], "task_type", f"task_type:{normalized}"

    # Keyword routing is opt-in because arbitrary context must not silently raise cost.
    if routing.get("keyword_fallback_enabled") is True:
        keyword_tiers = routing.get("keyword_tiers")
        if not isinstance(keyword_tiers, dict):
            raise ValueError("keyword_tiers must be an object when keyword fallback is enabled")
        text = _selection_text(goal, context)
        for tier in _TIERS:
            keywords = keyword_tiers.get(tier, [])
            if not isinstance(keywords, list) or not all(isinstance(item, str) for item in keywords):
                raise ValueError("keyword_tiers values must be string lists")
            for keyword in keywords:
                if keyword.lower() in text:
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
        raise ValueError(f"host trust policy {key} does not allow the selected route value")


def _number(value: Any, name: str, *, minimum: float = 0.0) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{name} must be a number >= {minimum}")
    return float(value)


def _route_catalog(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if config.get("catalog_version") != _CATALOG_VERSION:
        raise ValueError(f"catalog_version must be {_CATALOG_VERSION}")
    routes = config.get("routes")
    if not isinstance(routes, dict) or set(routes) != set(_TIERS):
        raise ValueError("routes must define exactly: luna, luna_economy, terra, sol")
    catalog: dict[str, dict[str, Any]] = {}
    for tier in _TIERS:
        route = routes[tier]
        if not isinstance(route, dict):
            raise ValueError(f"route {tier!r} must be an object")
        provider, model = route.get("provider"), route.get("model")
        if not isinstance(provider, str) or not provider.strip() or not isinstance(model, str) or not model.strip():
            raise ValueError(f"route {tier!r} must declare provider and model")
        enabled = route.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"route {tier!r} enabled must be true or false")
        pricing = route.get("pricing")
        if not isinstance(pricing, dict):
            raise ValueError(f"route {tier!r} requires pricing")
        input_price = _number(pricing.get("input_per_million_usd"), f"route {tier!r} input price")
        output_price = _number(pricing.get("output_per_million_usd"), f"route {tier!r} output price")
        max_output_tokens = _number(route.get("max_output_tokens"), f"route {tier!r} max_output_tokens", minimum=1)
        fallbacks = route.get("budget_fallbacks", list(_DEFAULT_FALLBACKS[tier]))
        if not isinstance(fallbacks, list) or any(item not in _TIERS or item == tier for item in fallbacks):
            raise ValueError(f"route {tier!r} budget_fallbacks must contain known distinct tiers")
        catalog[tier] = {
            "enabled": enabled,
            "provider": provider.strip(),
            "model": model.strip(),
            "worker_profile": route.get("worker_profile", _WORKER_PROFILES[tier]),
            "input_per_million_usd": input_price,
            "output_per_million_usd": output_price,
            "max_output_tokens": int(max_output_tokens),
            "budget_fallbacks": fallbacks,
        }
    return catalog


def _authorize_route(tier: str, config: dict[str, Any], catalog: dict[str, dict[str, Any]]) -> str:
    """Validate the selected catalog pair and return its bound worker profile."""
    route = catalog[tier]
    if not route["enabled"]:
        raise ValueError(f"route {tier!r} is disabled by operator configuration")
    llm = config.get("llm")
    if not isinstance(llm, dict):
        raise ValueError("operator configuration is missing host LLM trust policy")
    if llm.get("allow_provider_override") is not True or llm.get("allow_model_override") is not True:
        raise ValueError("host trust policy must explicitly allow provider and model overrides")
    _required_allowlist(llm, "allowed_providers", route["provider"])
    _required_allowlist(llm, "allowed_models", route["model"])
    profile = route.get("worker_profile", _WORKER_PROFILES[tier])
    if profile != _WORKER_PROFILES[tier]:
        raise ValueError(f"route {tier!r} worker_profile must be {_WORKER_PROFILES[tier]!r}")
    return profile


def _estimate(route: dict[str, Any], input_tokens: int, output_tokens: int | None = None) -> dict[str, Any]:
    output = route["max_output_tokens"] if output_tokens is None else output_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output,
        "total_tokens": input_tokens + output,
        "cost_usd": round((input_tokens * route["input_per_million_usd"] + output * route["output_per_million_usd"]) / 1_000_000, 8),
    }


def _budget_limits(args: dict[str, Any], config: dict[str, Any]) -> tuple[float | None, int | None, float | None]:
    budget = config.get("budget", {})
    if not isinstance(budget, dict):
        raise ValueError("budget must be an object when supplied")
    max_cost = args.get("max_cost_usd", budget.get("max_cost_usd"))
    max_tokens = args.get("max_tokens", budget.get("max_tokens"))
    remaining = args.get("remaining_budget_usd", budget.get("remaining_budget_usd"))
    return (
        None if max_cost is None else _number(max_cost, "max_cost_usd"),
        None if max_tokens is None else int(_number(max_tokens, "max_tokens", minimum=1)),
        None if remaining is None else _number(remaining, "remaining_budget_usd"),
    )


def _choose_budget_route(
    selected: str,
    catalog: dict[str, dict[str, Any]],
    input_tokens: int,
    max_cost: float | None,
    max_tokens: int | None,
    remaining: float | None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    candidates = [selected, *catalog[selected]["budget_fallbacks"]]
    attempted: list[dict[str, Any]] = []
    for tier in candidates:
        route = catalog[tier]
        estimate = _estimate(route, input_tokens)
        reason = None
        if not route["enabled"]:
            reason = "disabled"
        elif max_cost is not None and estimate["cost_usd"] > max_cost:
            reason = "max_cost_usd"
        elif remaining is not None and estimate["cost_usd"] > remaining:
            reason = "remaining_budget_usd"
        elif max_tokens is not None and estimate["total_tokens"] > max_tokens:
            reason = "max_tokens"
        attempted.append({"tier": tier, "estimate": estimate, "rejected_by": reason})
        if reason is None:
            return tier, estimate, {"selected_tier": tier, "fallback_applied": tier != selected, "attempted": attempted}
    raise ValueError("no configured route fits the requested budget: " + json.dumps(attempted))


def _prompt(goal: str, context: str | None, tier: str, task_type: str | None) -> str:
    parts = [
        "You are a bounded worker invoked by a controller-first local routing plugin.",
        "Return evidence, a draft, or local analysis only.",
        "Do not make final publication, merge, deployment, production, configuration, blocker, risk-acceptance, or stage-promotion decisions.",
        "State unverified items explicitly.",
        f"Tier: {tier}; task_type: {task_type or 'auto'}.",
        "", "Goal:", goal.strip(),
    ]
    if context and context.strip():
        parts.extend(["", "Context:", context.strip()])
    return "\n".join(parts)


def _utf8_prefix(value: str, max_bytes: int) -> tuple[str, bool]:
    """Return the largest UTF-8-safe prefix that fits ``max_bytes``."""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


def _task_body(
    *,
    goal: str,
    context: str,
    tier: str,
    task_type: str | None,
    route: dict[str, Any],
    estimate: dict[str, Any],
    budget: dict[str, Any],
    truncation: dict[str, bool],
) -> tuple[str, dict[str, bool]]:
    """Build a complete Kanban brief within the worker's 8 KiB body contract."""
    goal_text, context_text = goal.strip(), context.strip()
    actual_truncation = dict(truncation)

    def render() -> str:
        payload = {
            "route": tier,
            "task_type": task_type or "auto",
            "catalog_provider": route["provider"],
            "catalog_model": route["model"],
            "estimated_usage": estimate,
            "budget": budget,
            "truncated": actual_truncation,
            "acceptance": [
                "Perform the bounded task in the assigned workspace using Hermes tools.",
                "Return concrete artifact paths and commands/tests actually run.",
                "Do not make publication, merge, deployment, production, configuration, or risk-acceptance decisions.",
            ],
        }
        parts = [
            "You are a Kanban-dispatched worker with real Hermes tools.",
            "", "Goal:", goal_text,
        ]
        if context_text:
            parts.extend(["", "Context:", context_text])
        parts.extend(["", "Routing metadata:", json.dumps(payload, ensure_ascii=False)])
        return "\n".join(parts)

    while True:
        body = render()
        excess = len(body.encode("utf-8")) - _MAX_TASK_BODY_BYTES
        if excess <= 0:
            return body, actual_truncation
        if context_text:
            context_text, _ = _utf8_prefix(context_text, max(0, len(context_text.encode("utf-8")) - excess))
            actual_truncation["context"] = True
            continue
        if goal_text:
            goal_text, _ = _utf8_prefix(goal_text, max(0, len(goal_text.encode("utf-8")) - excess))
            actual_truncation["goal"] = True
            continue
        raise ValueError("Kanban routing metadata exceeds the worker body limit")


def _task_title(goal: str, tier: str) -> str:
    compact = " ".join(goal.split())
    return f"[{tier}] {compact[:120]}" or f"[{tier}] Routed task"


def _idempotency_key(
    args: dict[str, Any],
    tier: str,
    goal: str,
    context: str,
    *,
    board: str | None,
    workspace_kind: str,
    workspace_path: str | None,
) -> str:
    supplied = args.get("idempotency_key")
    if supplied is not None and (not isinstance(supplied, str) or not supplied.strip()):
        raise ValueError("idempotency_key must be a non-empty string when supplied")
    # Kanban deduplicates globally by key, so scope both caller retry keys and
    # router-derived keys to the resolved board/workspace identity.
    import hashlib
    material = "\0".join((
        "supplied" if supplied is not None else "derived",
        tier,
        board or "default",
        workspace_kind,
        workspace_path or "",
        supplied.strip() if supplied is not None else goal,
        "" if supplied is not None else context,
    ))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{_PLUGIN_ID}:{digest}"


def _handler(ctx, args: dict[str, Any], **_: Any) -> str:
    goal = args.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        return _error("ruoyu_cost_router requires a non-empty goal")
    context, route, task_type, project = args.get("context"), args.get("route"), args.get("task_type"), args.get("project")
    if context is not None and not isinstance(context, str):
        return _error("context must be a string when supplied")
    if route is not None and not isinstance(route, str):
        return _error("route must be a string when supplied")
    if task_type is not None and not isinstance(task_type, str):
        return _error("task_type must be a string when supplied")
    if project is not None and not isinstance(project, dict):
        return _error("project must be an object when supplied")

    bounded_goal, goal_truncated = _bounded_text(_safe_text(goal), _MAX_GOAL_CHARS)
    bounded_context, context_truncated = _bounded_text(_safe_text(context or ""), _MAX_CONTEXT_CHARS)
    truncation = {"goal": goal_truncated, "context": context_truncated, "output": False}
    try:
        config = _plugin_config()
        catalog = _route_catalog(config)
        routing = config.get("routing", {})
        if not isinstance(routing, dict):
            raise ValueError("routing must be an object when supplied")
        if task_type is not None and task_type not in _TASK_TYPE_TIERS:
            raise ValueError(f"unsupported task_type: {task_type}")
        requested_tier, selection_mode, matched_rule = _select_tier(route, bounded_goal, bounded_context, task_type, project, routing)
        max_cost, max_tokens, remaining = _budget_limits(args, config)
        input_tokens = max(1, len(_prompt(bounded_goal, bounded_context, requested_tier, task_type)) // _CHARS_PER_TOKEN)
        tier, estimate, budget = _choose_budget_route(requested_tier, catalog, input_tokens, max_cost, max_tokens, remaining)
        worker_profile = _authorize_route(tier, config, catalog)
        workspace_path = args.get("workspace_path") or (project or {}).get("workdir")
        workspace_kind = args.get("workspace_kind") or ("dir" if workspace_path else "scratch")
        if workspace_kind not in {"scratch", "dir", "worktree"}:
            raise ValueError("workspace_kind must be one of: scratch, dir, worktree")
        if workspace_kind == "scratch" and workspace_path is not None:
            raise ValueError("workspace_path is not allowed for an isolated scratch workspace")
        if workspace_kind in {"dir", "worktree"} and (not isinstance(workspace_path, str) or not workspace_path.startswith("/")):
            raise ValueError("workspace_path must be an absolute path for dir or worktree workspaces")
        task_body, truncation = _task_body(
            goal=bounded_goal, context=bounded_context, tier=tier, task_type=task_type,
            route=catalog[tier], estimate=estimate, budget=budget, truncation=truncation,
        )
        task_args = {
            "title": _task_title(bounded_goal, tier),
            "body": task_body,
            "assignee": worker_profile,
            "workspace_kind": workspace_kind,
            "workspace_path": workspace_path,
            "board": args.get("board"),
            "priority": args.get("priority", 0),
            "max_runtime_seconds": args.get("max_runtime_seconds", 900),
            "idempotency_key": _idempotency_key(
                args, tier, bounded_goal, bounded_context,
                board=args.get("board"), workspace_kind=workspace_kind, workspace_path=workspace_path,
            ),
            "skills": args.get("skills", []),
        }
        # Ensure the built-in Kanban registration module is loaded before using
        # PluginContext's public registry-dispatch facade. Full agents normally
        # load it at startup, but plugins must not rely on import order.
        import tools.kanban_tools  # noqa: F401
        task_result = json.loads(ctx.dispatch_tool("kanban_create", task_args))
        if not task_result.get("ok"):
            raise ValueError(task_result.get("error", "Kanban task creation failed"))
    except ValueError as exc:
        return _error(str(exc), tier=locals().get("tier"))
    except Exception as exc:
        return _error(f"host-owned Kanban task creation failed: {exc}", tier=locals().get("tier"))

    task_id = task_result["task_id"]
    task_status = task_result.get("status")
    created = task_result.get("created")
    routing_status = "queued" if created is True else "existing"
    return _safe_json({
        "routing_status": routing_status,
        "task_id": task_id,
        "task_status": task_status,
        "board": args.get("board"),
        "tier": tier,
        "route": tier,
        "requested_tier": requested_tier,
        "worker_profile": worker_profile,
        "selection_mode": selection_mode,
        "matched_rule": matched_rule,
        "provider": catalog[tier]["provider"],
        "model": catalog[tier]["model"],
        "truncated": truncation,
        "budget": {**budget, "limits": {"max_cost_usd": max_cost, "max_tokens": max_tokens, "remaining_budget_usd": remaining}},
        "usage": {"estimated": estimate, "actual": None},
        "worker_result": {"status": routing_status, "deliverable": None, "evidence": [], "unverified_items": ["Wait for the Kanban worker result, then controller must independently verify it."], "controller_decisions": ["Controller retains final acceptance and all irreversible decisions."]},
        "controller_decision_required": True,
    })


_SCHEMA = {
    "name": "ruoyu_cost_router",
    "description": "Route one bounded request through a validated, budget-aware local plugin catalog. The controller retains final decisions.",
    "parameters": {"type": "object", "properties": {
        "goal": {"type": "string", "description": "Self-contained bounded task and deliverable."},
        "context": {"type": "string", "description": "Optional background needed for the route."},
        "route": {"type": "string", "enum": list(_TIERS), "description": "Optional explicit operator-defined tier."},
        "task_type": {"type": "string", "enum": sorted(_TASK_TYPE_TIERS), "description": "Optional restricted routing category."},
        "project": {"type": "object", "description": "Optional routing metadata; route, route_candidate, task_type, and workdir are recognized."},
        "workspace_kind": {"type": "string", "enum": ["scratch", "dir", "worktree"], "description": "Kanban worker workspace flavor; defaults to dir when a workdir is supplied, otherwise scratch."},
        "workspace_path": {"type": "string", "description": "Absolute workspace path for a dir/worktree worker task."},
        "board": {"type": "string", "description": "Optional durable Kanban board slug."},
        "priority": {"type": "integer", "description": "Optional Kanban dispatcher priority."},
        "max_runtime_seconds": {"type": "integer", "minimum": 1, "description": "Maximum worker runtime; defaults to 900 seconds."},
        "skills": {"type": "array", "items": {"type": "string"}, "description": "Optional skills to force-load in the assigned worker."},
        "idempotency_key": {"type": "string", "description": "Optional retry-safe Kanban task key."},
        "max_cost_usd": {"type": "number", "minimum": 0, "description": "Per-task maximum estimated cost in USD."},
        "max_tokens": {"type": "integer", "minimum": 1, "description": "Per-call maximum estimated total tokens."},
        "remaining_budget_usd": {"type": "number", "minimum": 0, "description": "Externally tracked remaining budget available to this call."},
    }, "required": ["goal"]},
}


def register(ctx) -> None:
    ctx.register_tool(name="ruoyu_cost_router", toolset="delegation", schema=_SCHEMA, handler=lambda args, **kwargs: _handler(ctx, args, **kwargs), emoji="R")
