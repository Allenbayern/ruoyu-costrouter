"""Private controller-first, budget-aware routing plugin.

The operator-owned route catalog is validated before every dispatch. The plugin
uses the host-owned ``ctx.llm`` facade and never resolves profiles or shells out.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from agent.redact import redact_sensitive_text
from hermes_cli.config import load_config

_PLUGIN_ID = "ruoyu-cost-router"
_CATALOG_VERSION = 5
_TIERS = ("flash", "luna", "terra", "terra_pro", "sol")
_UPGRADE_TASK_TYPES = {"architecture", "cross_artifact_analysis", "difficult_debugging"}
_WORKER_PROFILES = {
    "flash": "worker-flash",
    "luna": "worker-luna",
    "terra": "worker-terra",
    "terra_pro": "worker-terra",
    "sol": "worker-sol",
}
# Route selection is explicit. No task type may imply a cross-model fallback:
# this map is a direct route binding. `terra_pro` is never an automatic
# selection target: it is reachable only by an explicit `route: terra_pro`.
_TASK_TYPE_TIERS = {
    "router": "flash",
    "classify": "flash",
    "dedupe": "flash",
    "normalize": "flash",
    "fixed_extraction": "flash",
    "cleanup": "luna",
    "coding": "terra",
    "rag_answer": "terra",
    "evidence_synthesis": "terra",
    "repair": "terra",
    "draft": "terra",
    "final_chinese": "terra",
    "architecture": "terra",
    "cross_artifact_analysis": "terra",
    "difficult_debugging": "terra",
    "final_review": "sol",
}
_MAX_GOAL_CHARS = 12_000
_MAX_CONTEXT_CHARS = 12_000
_MAX_OUTPUT_CHARS = 12_000
_MAX_TASK_BODY_BYTES = 8 * 1024  # Hermes Kanban worker-context contract.
_REVIEW_PACKET_MANIFEST_VERSION = 1
_REVIEW_PACKET_TRANSPORT = "task_attachment"
_DEFAULT_REVIEW_INPUT_TOKENS = 60_000
_MAX_REVIEW_INPUT_TOKENS = 120_000
_CHARS_PER_TOKEN = 4
_REVIEWER_LIMITS = {"sol": 2, "luna": 3}
_REVIEWER_PROFILES = {"sol": "worker-sol", "luna": "worker-luna"}
_REVIEW_STAGES = {"review", "protected_final"}
_CONTROLLER_RECOVERY_AUTHORITY = "controller-recovery-v1"
_ROOT_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_LOGICAL_ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")
_PLUGIN_DIR = Path(__file__).resolve().parent
_TEST_CONTROLLER_CONTEXT = object()


class _BudgetExceededError(ValueError):
    """Signal a selected-route budget refusal without conflating validation errors."""


def _safe_text(value: Any, limit: int | None = None) -> str:
    try:
        text = redact_sensitive_text(str(value or ""), force=True)
    except Exception:
        return "[REDACTED - redaction failed]"
    return text[:limit] if limit is not None else text


def _safe_json(payload: dict[str, Any]) -> str:
    return _safe_text(json.dumps(payload, ensure_ascii=False), limit=None)


def _error(
    message: str, *, tier: str | None = None, route: str | None = None,
    budget: dict[str, Any] | None = None,
) -> str:
    result: dict[str, Any] = {
        "routing_status": "denied",
        "controller_decision_required": True,
        "error": _safe_text(message, limit=2_000),
    }
    if tier:
        result["tier"] = tier
    if route:
        result["route"] = route
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


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _read_packet_attachment(path: Path | None, label: str) -> tuple[str, int]:
    if path is None:
        raise ValueError(f"{label} attachment is missing")
    try:
        value = path.read_bytes()
    except (OSError, ValueError) as exc:
        raise ValueError(f"{label} attachment is unreadable") from exc
    return _sha256_bytes(value), len(value)


def _manifest_ascii(payload: dict[str, Any]) -> str:
    try:
        manifest = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        manifest.encode("ascii")
    except (TypeError, UnicodeEncodeError) as exc:
        raise ValueError("review packet manifest must be ASCII-serializable") from exc
    if len(manifest.encode("ascii")) > _MAX_TASK_BODY_BYTES:
        raise ValueError("review packet manifest exceeds 8192 bytes; refusing incomplete locator")
    return manifest


def _estimate_review_input_tokens(estimated_tokens: int | None = None) -> int:
    """Validate the conservative F4 input-estimate data contract.

    The router has no production packet tokenizer or controller authority
    issuer, so this is deliberately only a pure estimate contract. Callers
    that omit an estimate receive the conservative 60k default; callers above
    that threshold must later present a controller-verifiable exception.
    """
    if estimated_tokens is None:
        return _DEFAULT_REVIEW_INPUT_TOKENS
    if not isinstance(estimated_tokens, int) or isinstance(estimated_tokens, bool) or estimated_tokens < 1:
        raise ValueError("review packet input_tokens must be a positive integer")
    if estimated_tokens > _MAX_REVIEW_INPUT_TOKENS:
        raise ValueError("review packet input_tokens exceeds 120000")
    return estimated_tokens


def _manifest_payload_digest(locator: dict[str, Any]) -> str:
    """Digest the canonical locator excluding its non-self-referential checksum."""
    return _sha256_bytes(_manifest_ascii({key: value for key, value in locator.items() if key != "manifest_sha256"}).encode("ascii"))


def _prepare_review_packet(packet: Any) -> dict[str, Any]:
    """Build a complete, bounded locator without creating a Kanban task.

    This deliberately prepares only immutable attachment facts. Public Kanban
    creation cannot attach blobs before a task becomes visible, so operational
    dispatch must remain closed until the host exposes an atomic contract.
    """
    if not isinstance(packet, dict) or set(packet) - {"version", "semantic_review_key", "packet", "sources", "input_tokens", "authority_record_id"}:
        raise ValueError("review packet has an invalid schema")
    if packet.get("version") != _REVIEW_PACKET_MANIFEST_VERSION:
        raise ValueError("review packet version is unsupported")
    semantic_key = packet.get("semantic_review_key")
    if not isinstance(semantic_key, str) or not _SHA256_RE.fullmatch(semantic_key):
        raise ValueError("review packet semantic_review_key is malformed")
    input_tokens = _estimate_review_input_tokens(packet.get("input_tokens"))
    raw_packet = packet.get("packet")
    if not isinstance(raw_packet, dict) or set(raw_packet) != {"logical_id", "attachment_id", "path"}:
        raise ValueError("review packet attachment has an invalid schema")
    packet_digest, packet_bytes = _read_packet_attachment(Path(raw_packet["path"]) if isinstance(raw_packet.get("path"), str) else None, "packet")
    if not all(isinstance(raw_packet[key], str) and raw_packet[key].strip() for key in ("logical_id", "attachment_id")):
        raise ValueError("review packet attachment identity is malformed")
    sources: list[dict[str, Any]] = []
    raw_sources = packet.get("sources")
    if not isinstance(raw_sources, list):
        raise ValueError("review packet sources must be a list")
    for source in raw_sources:
        if not isinstance(source, dict) or set(source) != {"logical_id", "attachment_id", "path", "relevance", "omission"}:
            raise ValueError("review packet source has an invalid schema")
        if not all(isinstance(source[key], str) and source[key].strip() for key in ("logical_id", "attachment_id", "relevance", "omission")):
            raise ValueError("review packet source fields are malformed")
        digest, byte_count = _read_packet_attachment(Path(source["path"]) if isinstance(source.get("path"), str) else None, "source")
        sources.append({key: source[key] for key in ("logical_id", "attachment_id", "relevance", "omission")} | {"sha256": digest, "bytes": byte_count})
    if len({source["logical_id"] for source in sources}) != len(sources):
        raise ValueError("review packet source logical_ids must be unique")
    authority_record_id = packet.get("authority_record_id")
    if authority_record_id is not None and (not isinstance(authority_record_id, str) or not authority_record_id.strip()):
        raise ValueError("review packet authority_record_id is malformed")
    locator = {"version": _REVIEW_PACKET_MANIFEST_VERSION, "transport": _REVIEW_PACKET_TRANSPORT,
               "semantic_review_key": semantic_key,
               "packet": {"logical_id": raw_packet["logical_id"], "attachment_id": raw_packet["attachment_id"], "sha256": packet_digest, "bytes": packet_bytes},
               "sources": sources, "input_tokens": input_tokens}
    if authority_record_id is not None:
        locator["authority_record_id"] = authority_record_id
    locator["manifest_sha256"] = _manifest_payload_digest(locator)
    manifest = _manifest_ascii(locator)
    return {"manifest": manifest, "manifest_digest": _sha256_bytes(manifest.encode("ascii")),
            "input_tokens": input_tokens, "packet": locator["packet"], "sources": sources,
            "semantic_review_key": semantic_key, "authority_record_id": authority_record_id}


def _validate_review_packet_manifest(
    manifest: str,
    *,
    packet_path: Path | None,
    source_paths: dict[str, Path],
    ledger_path: Path | None = None,
    board: str = "default",
) -> dict[str, Any]:
    if not isinstance(manifest, str) or not manifest.isascii() or len(manifest.encode("ascii")) > _MAX_TASK_BODY_BYTES:
        raise ValueError("review packet manifest is missing, non-ASCII, or exceeds 8192 bytes")
    try:
        locator = json.loads(manifest)
    except json.JSONDecodeError as exc:
        raise ValueError("review packet manifest is truncated or malformed") from exc
    if _manifest_ascii(locator) != manifest or locator.get("version") != _REVIEW_PACKET_MANIFEST_VERSION or locator.get("transport") != _REVIEW_PACKET_TRANSPORT:
        raise ValueError("review packet manifest is incomplete or non-canonical")
    manifest_payload_digest = locator.get("manifest_sha256")
    if not isinstance(manifest_payload_digest, str) or not _SHA256_RE.fullmatch(manifest_payload_digest) or manifest_payload_digest != _manifest_payload_digest(locator):
        raise ValueError("review packet manifest digest is missing or mismatched")
    semantic_key = locator.get("semantic_review_key")
    if not isinstance(semantic_key, str) or not _SHA256_RE.fullmatch(semantic_key):
        raise ValueError("review packet manifest semantic_review_key is malformed")
    packet = locator.get("packet")
    if not isinstance(packet, dict) or set(packet) != {"logical_id", "attachment_id", "sha256", "bytes"}:
        raise ValueError("review packet manifest packet is incomplete")
    digest, byte_count = _read_packet_attachment(packet_path, "packet")
    if packet.get("sha256") != digest or packet.get("bytes") != byte_count:
        raise ValueError("packet attachment digest mismatch")
    sources = locator.get("sources")
    if not isinstance(sources, list):
        raise ValueError("review packet manifest sources are missing")
    for source in sources:
        if not isinstance(source, dict) or set(source) != {"logical_id", "attachment_id", "relevance", "omission", "sha256", "bytes"}:
            raise ValueError("review packet manifest source is incomplete")
        logical_id = source.get("logical_id")
        source_digest, source_bytes = _read_packet_attachment(source_paths.get(logical_id) if isinstance(logical_id, str) else None, "source")
        if source.get("sha256") != source_digest or source.get("bytes") != source_bytes:
            raise ValueError("source attachment digest mismatch")
    try:
        input_tokens = _estimate_review_input_tokens(locator.get("input_tokens"))
    except ValueError as exc:
        raise ValueError("review packet manifest input_tokens is outside allowed bounds") from exc
    authority_record_id = locator.get("authority_record_id")
    manifest_digest = _sha256_bytes(manifest.encode("ascii"))
    if input_tokens > _DEFAULT_REVIEW_INPUT_TOKENS:
        if not isinstance(authority_record_id, str) or ledger_path is None or not _verify_budget_exception(ledger_path, authority_record_id, board, semantic_key, manifest_digest, input_tokens):
            raise ValueError("review packet budget exception is absent or unverifiable")
    return {"manifest": manifest, "manifest_digest": manifest_digest, "input_tokens": input_tokens,
            "packet": packet, "sources": sources, "semantic_review_key": semantic_key,
            "authority_record_id": authority_record_id}


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
    project = project or {}
    candidates = [("route", route), ("project.route", project.get("route")),
                  ("project.route_candidate", project.get("route_candidate"))]
    normalized_routes = [(source, str(value).strip().lower().replace("-", "_")) for source, value in candidates if value is not None]
    if normalized_routes:
        values = {value for _, value in normalized_routes}
        if len(values) != 1:
            raise ValueError("route sources conflict")
        tier = normalized_routes[0][1]
        if tier not in _TIERS:
            raise ValueError("route must be one of: " + ", ".join(_TIERS))
    else:
        tier = None
    task_candidates = [("task_type", task_type), ("project.task_type", project.get("task_type"))]
    normalized_tasks = [(source, str(value).strip().lower().replace("-", "_")) for source, value in task_candidates if value is not None]
    if normalized_tasks and len({value for _, value in normalized_tasks}) != 1:
        raise ValueError("task_type sources conflict")
    task = normalized_tasks[0][1] if normalized_tasks else None
    if task is not None and task not in _TASK_TYPE_TIERS:
        raise ValueError("task_type must be one of: " + ", ".join(sorted(_TASK_TYPE_TIERS)))
    if tier is not None and task is not None and tier != _TASK_TYPE_TIERS[task]:
        # Explicit terra_pro is the only route/task-type mismatch permitted:
        # it is an explicit quality upgrade for the named upgrade task classes.
        if not (tier == "terra_pro" and task in _UPGRADE_TASK_TYPES):
            raise ValueError("route and task_type sources conflict")
    if tier is not None:
        return tier, "route", f"route:{tier}"
    if task is not None:
        return _TASK_TYPE_TIERS[task], "task_type", f"task_type:{task}"
    raise ValueError("route or task_type is required; no default route is permitted")


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
    """Parse the sole active v5 flat route catalog, failing closed on drift."""
    if config.get("catalog_version") != _CATALOG_VERSION:
        raise ValueError(f"catalog_version must be {_CATALOG_VERSION}")
    routes = config.get("routes")
    if not isinstance(routes, dict) or set(routes) != set(_TIERS):
        raise ValueError("routes must define exactly: " + ", ".join(_TIERS))
    allowed = {"enabled", "provider", "model", "worker_profile", "pricing", "max_output_tokens", "budget_fallbacks"}
    catalog: dict[str, dict[str, Any]] = {}
    for tier in _TIERS:
        route = routes[tier]
        if not isinstance(route, dict):
            raise ValueError(f"route {tier!r} must be an object")
        unknown = sorted(set(route) - allowed)
        if unknown:
            raise ValueError("route " + repr(tier) + " has unsupported fields: " + ", ".join(unknown))
        provider, model = route.get("provider"), route.get("model")
        if not isinstance(provider, str) or not provider.strip() or not isinstance(model, str) or not model.strip():
            raise ValueError(f"route {tier!r} must declare provider and model")
        enabled = route.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"route {tier!r} enabled must be true or false")
        pricing = route.get("pricing")
        if not isinstance(pricing, dict):
            raise ValueError(f"route {tier!r} requires pricing")
        fallbacks = route.get("budget_fallbacks")
        if fallbacks != []:
            raise ValueError(f"route {tier!r} budget_fallbacks must be empty")
        catalog[tier] = {
            "enabled": enabled, "provider": provider.strip(), "model": model.strip(),
            "worker_profile": route.get("worker_profile", _WORKER_PROFILES[tier]),
            "input_per_million_usd": _number(pricing.get("input_per_million_usd"), f"route {tier!r} input price"),
            "output_per_million_usd": _number(pricing.get("output_per_million_usd"), f"route {tier!r} output price"),
            "max_output_tokens": int(_number(route.get("max_output_tokens"), f"route {tier!r} max_output_tokens", minimum=1)),
            "budget_fallbacks": [],
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


def _budget_candidates(
    selected: str,
    selected_route: dict[str, Any],
    catalog: dict[str, dict[str, Any]],
) -> list[tuple[str, dict[str, Any]]]:
    """Return exactly the selected route; flat v5 forbids budget fallbacks."""
    del catalog
    return [(selected, selected_route)]


def _choose_budget_route(
    selected: str,
    selected_route: dict[str, Any],
    catalog: dict[str, dict[str, Any]],
    input_tokens: int,
    max_cost: float | None,
    max_tokens: int | None,
    remaining: float | None,
) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, Any], str | None]:
    attempted: list[dict[str, Any]] = []
    for tier, route in _budget_candidates(selected, selected_route, catalog):
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
            return tier, route, estimate, {"selected_tier": tier, "fallback_applied": False, "attempted": attempted}, None
    raise _BudgetExceededError("no configured route fits the requested budget: " + json.dumps(attempted))


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


def _review_error(message: str, *, contract: dict[str, Any] | None = None) -> str:
    result: dict[str, Any] = {
        "routing_status": "denied",
        "admission_status": "denied",
        "controller_decision_required": True,
        "error": _safe_text(message, limit=2_000),
    }
    if contract is not None:
        result["controller_handoff_contract"] = contract
    return _safe_json(result)


def _sol_admission_error(message: str) -> str:
    """Return the common controller handoff for rejected Sol selections."""
    return _review_error(message, contract={
        "decision": "deny_controller_handoff",
        "required_assignee": "controller",
        "fallback_allowed": False,
    })


def _controller_handoff_error(message: str) -> str:
    """Fail closed when request data cannot establish controller authority."""
    result = json.loads(_sol_admission_error(message))
    result["admission_status"] = "deny_controller_handoff"
    return _safe_json(result)


def _normalize_sol_admission(
    *,
    requested_tier: str,
    review: dict[str, Any] | None,
) -> str:
    """Return the only decision permitted to select the Sol worker."""
    if requested_tier != "sol" and review is None:
        return "not_sol"
    if review is None:
        raise ValueError("Sol admission requires validated hard-L2 protected-final review metadata")
    if review["role"] == "sol" and review["risk"] == "hard-L2" and review["stage"] == "protected_final":
        return "sol_hard_l2"
    if review["role"] == "luna" and review["risk"] != "hard-L2" and review["stage"] == "review":
        return "luna_l1"
    raise ValueError("Sol admission requires role=sol, risk=hard-L2, and stage=protected_final")


def _artifact_digest(path: str) -> str:
    artifact = Path(path).resolve()
    if not artifact.is_file():
        raise ValueError("review artifact_ref must resolve to a regular local file")
    digest = hashlib.sha256()
    with artifact.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _validate_artifact_identity(artifact_ref: Any, artifact_digest: Any, *, field_prefix: str) -> None:
    if (artifact_ref is None) != (artifact_digest is None):
        raise ValueError(f"{field_prefix}.artifact_ref and artifact_digest must be supplied together")
    if artifact_ref is None:
        raise ValueError(f"{field_prefix} requires artifact_ref and artifact_digest")
    if not isinstance(artifact_ref, str) or not artifact_ref.strip():
        raise ValueError(f"{field_prefix}.artifact_ref must be a non-empty string")
    if not isinstance(artifact_digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact_digest):
        raise ValueError(f"{field_prefix}.artifact_digest must be sha256:<64 lowercase hex>")
    if _artifact_digest(artifact_ref) != artifact_digest:
        raise ValueError(f"{field_prefix}.artifact_digest does not match artifact_ref")


def _canonical_json_digest(payload: dict[str, Any]) -> str:
    """Hash UTF-8 canonical JSON without admitting transport/provenance fields."""
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _canonical_logical_artifacts(value: Any, *, field_prefix: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field_prefix}.logical_artifacts must be a non-empty list")
    artifacts: list[dict[str, Any]] = []
    logical_ids: set[str] = set()
    for artifact in value:
        if not isinstance(artifact, dict) or set(artifact) != {"logical_id", "sha256", "byte_count"}:
            raise ValueError(f"{field_prefix}.logical_artifacts entries must contain only logical_id, sha256, and byte_count")
        logical_id, digest, byte_count = artifact["logical_id"], artifact["sha256"], artifact["byte_count"]
        if not isinstance(logical_id, str) or not _LOGICAL_ARTIFACT_ID_RE.fullmatch(logical_id):
            raise ValueError(f"{field_prefix} identity logical_id is malformed")
        if logical_id in logical_ids:
            raise ValueError(f"{field_prefix} identity logical_id is duplicated")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise ValueError(f"{field_prefix} identity sha256 is malformed")
        if not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count <= 0:
            raise ValueError(f"{field_prefix} identity byte_count must be a positive integer")
        logical_ids.add(logical_id)
        artifacts.append({"logical_id": logical_id, "sha256": digest, "byte_count": byte_count})
    return sorted(artifacts, key=lambda item: item["logical_id"])


def _semantic_review_key(identity: dict[str, Any], *, provenance: Any = None) -> str:
    """Derive F2 initial-review identity solely from stable semantic inputs.

    ``provenance`` is deliberately accepted but ignored to make the separation
    testable at this boundary; provenance is validated independently below.
    """
    del provenance
    if not isinstance(identity, dict) or set(identity) != {
        "logical_artifacts", "acceptance_criteria_version", "acceptance_criteria_sha256"
    }:
        raise ValueError("review identity must contain only logical_artifacts, acceptance_criteria_version, and acceptance_criteria_sha256")
    version = identity["acceptance_criteria_version"]
    digest = identity["acceptance_criteria_sha256"]
    if not isinstance(version, str) or not version.strip():
        raise ValueError("review identity acceptance_criteria_version must be a non-empty string")
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        raise ValueError("review identity acceptance_criteria_sha256 is malformed")
    return _canonical_json_digest({
        "logical_artifacts": _canonical_logical_artifacts(identity["logical_artifacts"], field_prefix="review identity"),
        "acceptance_criteria_version": version,
        "acceptance_criteria_sha256": digest,
    })


def _canonical_finding_ids(value: Any, *, field_name: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"re-review {field_name} must be a non-empty string list")
    normalized = sorted({item.strip() for item in value})
    if len(normalized) != len(value):
        raise ValueError(f"re-review {field_name} must not contain duplicates")
    return normalized


def _re_review_key(*, base_semantic_review_key: Any, prior_review_digest: Any,
                   accepted_finding_ids: Any, addressed_finding_ids: Any,
                   repaired_logical_artifacts: Any) -> str:
    """Derive a fail-closed finding-scoped F2 re-review identity."""
    if not isinstance(base_semantic_review_key, str) or not _SHA256_RE.fullmatch(base_semantic_review_key):
        raise ValueError("re-review base_semantic_review_key is malformed")
    if not isinstance(prior_review_digest, str) or not _SHA256_RE.fullmatch(prior_review_digest):
        raise ValueError("re-review prior_review_digest is malformed")
    accepted = _canonical_finding_ids(accepted_finding_ids, field_name="accepted_finding_ids")
    addressed = _canonical_finding_ids(addressed_finding_ids, field_name="addressed_finding_ids")
    if not set(addressed).issubset(accepted):
        raise ValueError("re-review addressed_finding_ids must be accepted finding IDs")
    repaired = _canonical_logical_artifacts(repaired_logical_artifacts, field_prefix="re-review")
    if any(item["sha256"] == prior_review_digest for item in repaired):
        raise ValueError("re-review repaired artifact digest must differ from prior review digest")
    return _canonical_json_digest({
        "base_semantic_review_key": base_semantic_review_key,
        "prior_review_digest": prior_review_digest,
        "accepted_finding_ids": accepted,
        "repaired_logical_artifacts": repaired,
    })


def _review_metadata(args: dict[str, Any]) -> dict[str, Any] | None:
    review = args.get("review")
    if review is None:
        return None
    if not isinstance(review, dict):
        raise ValueError("review must be an object")
    allowed_extra = {"parent_task_ids", "required_evidence_paths", "review_kind", "review_identity",
                     "base_semantic_review_key", "prior_review_digest", "accepted_finding_ids",
                     "addressed_finding_ids", "repaired_logical_artifacts", "artifact_ref", "artifact_digest",
                     "producer_runs", "producer_profiles", "authority_record_id",
                     "controller_authority_marker", "controller_recovery"}
    required = {"role", "risk", "stage", "root_key", "exclusions"}
    if set(review) - (required | allowed_extra):
        raise ValueError("review contains unsupported metadata fields: " + ", ".join(
            sorted(set(review) - required - allowed_extra)))
    if missing := sorted(required - set(review)):
        raise ValueError("review is missing protected metadata: " + ", ".join(missing))
    role, risk, stage, root_key = (review[name] for name in ("role", "risk", "stage", "root_key"))
    if not isinstance(role, str) or role not in _REVIEWER_LIMITS:
        raise ValueError("review.role must be sol or luna")
    if not isinstance(risk, str) or risk not in {"low", "medium", "high", "hard-L2"}:
        raise ValueError("review.risk must be low, medium, high, or hard-L2")
    if not isinstance(stage, str) or stage not in _REVIEW_STAGES:
        raise ValueError("review.stage must be review or protected_final")
    if not isinstance(root_key, str) or not _ROOT_KEY_RE.fullmatch(root_key):
        raise ValueError("review.root_key is malformed")
    for name in ("exclusions",):
        value = review[name]
        if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
            raise ValueError(f"review.{name} must be a non-empty list of non-empty strings")
    controller_recovery = review.get("controller_recovery")
    has_runs = review.get("producer_runs")
    has_profiles = review.get("producer_profiles")
    if controller_recovery is not None:
        if has_runs is not None or has_profiles is not None:
            raise ValueError("controller_recovery must not be combined with worker provenance")
        if not isinstance(controller_recovery, dict) or set(controller_recovery) != {
            "authority", "authority_record_id", "failed_task_id", "recovery_reason",
            "artifact_ref", "artifact_digest",
        }:
            raise ValueError("controller_recovery has an invalid schema")
        if controller_recovery["authority"] != _CONTROLLER_RECOVERY_AUTHORITY:
            raise ValueError("controller_recovery authority is unrecognized")
        for field in ("authority_record_id", "failed_task_id", "recovery_reason"):
            if not isinstance(controller_recovery[field], str) or not controller_recovery[field].strip():
                raise ValueError(f"controller_recovery.{field} must be a non-empty string")
        _validate_artifact_identity(
            controller_recovery["artifact_ref"], controller_recovery["artifact_digest"],
            field_prefix="controller_recovery",
        )
    # Support both producer_runs (v2) and producer_profiles (v1 compat).
    elif has_runs is not None and has_profiles is not None:
        raise ValueError("review: use producer_runs (not producer_profiles) in v2 mode")
    if controller_recovery is None:
        if has_runs is not None:
            if not isinstance(has_runs, list) or not has_runs:
                raise ValueError("review.producer_runs must be a non-empty list")
            required_run_fields = {
                "task_id", "run_id", "profile", "route", "provider", "model",
                "artifact_ref", "artifact_digest",
            }
            for run in has_runs:
                if not isinstance(run, dict):
                    raise ValueError("each producer_runs entry must be an object")
                unsupported = sorted(set(run) - required_run_fields)
                if unsupported:
                    raise ValueError("producer_runs entry has unsupported fields: " + ", ".join(unsupported))
                for field in ("task_id", "run_id", "profile", "route", "provider", "model"):
                    if not isinstance(run.get(field), str) or not run[field].strip():
                        raise ValueError(f"producer_runs entry missing required field {field!r}")
                if run["route"] not in _TIERS:
                    raise ValueError("producer_runs entry route must be one of: " + ", ".join(_TIERS))
                _validate_artifact_identity(run.get("artifact_ref"), run.get("artifact_digest"), field_prefix="producer_runs entry")
            _validate_artifact_identity(review.get("artifact_ref"), review.get("artifact_digest"), field_prefix="review")
        elif has_profiles is not None:
            if not isinstance(has_profiles, list) or not has_profiles or not all(isinstance(p, str) and p.strip() for p in has_profiles):
                raise ValueError("review.producer_profiles must be a non-empty list of strings")
        else:
            raise ValueError("review requires producer_runs (v2), producer_profiles (v1 compatibility), or controller_recovery")
    parents = review.get("parent_task_ids", [])
    evidence = review.get("required_evidence_paths", [])
    if not isinstance(parents, list) or not all(isinstance(item, str) and item.strip() for item in parents):
        raise ValueError("review.parent_task_ids must be a string list")
    if not isinstance(evidence, list) or not all(isinstance(item, str) and item.strip() for item in evidence):
        raise ValueError("review.required_evidence_paths must be a string list")
    if role in {"sol", "luna"} and role in review["exclusions"]:
        raise ValueError("review exclusions cannot exclude the selected review worker")
    if role == "sol" and stage != "protected_final":
        raise ValueError("Sol review cards must use protected_final stage")
    if stage == "protected_final" and role != "sol":
        raise ValueError("protected final review is Sol-only with no fallback")
    review_kind = review.get("review_kind")
    if review_kind is not None:
        if review_kind not in {"initial", "re_review"}:
            raise ValueError("review.review_kind must be initial or re_review")
        if review_kind == "initial":
            review["semantic_review_key"] = _semantic_review_key(review.get("review_identity"))
        else:
            review["semantic_review_key"] = _re_review_key(
                base_semantic_review_key=review.get("base_semantic_review_key"),
                prior_review_digest=review.get("prior_review_digest"),
                accepted_finding_ids=review.get("accepted_finding_ids"),
                addressed_finding_ids=review.get("addressed_finding_ids"),
                repaired_logical_artifacts=review.get("repaired_logical_artifacts"),
            )
    return review


def _ledger_path(config: dict[str, Any]) -> Path:
    value = config.get("reviewer_ledger_path", str(_PLUGIN_DIR / ".reviewer-admission.sqlite3"))
    if not isinstance(value, str) or not os.path.isabs(value):
        raise ValueError("reviewer_ledger_path must be an absolute path")
    path = Path(value).resolve()
    if _PLUGIN_DIR not in path.parents:
        raise ValueError("reviewer_ledger_path must remain inside the plugin directory")
    return path


def _ledger_connection(path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(path, timeout=5, isolation_level=None)
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("CREATE TABLE IF NOT EXISTS reviewer_admissions (reservation_id TEXT PRIMARY KEY, board TEXT NOT NULL, role TEXT NOT NULL, root_key TEXT NOT NULL, stage TEXT NOT NULL, task_id TEXT, state TEXT NOT NULL CHECK(state IN ('reserved','queued','quarantined')), created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, UNIQUE(board, root_key, stage))")
        connection.execute("CREATE TABLE IF NOT EXISTS reviewer_admission_semantics (reservation_id TEXT PRIMARY KEY REFERENCES reviewer_admissions(reservation_id), board TEXT NOT NULL, semantic_review_key TEXT NOT NULL, review_kind TEXT NOT NULL CHECK(review_kind IN ('initial','re_review')), UNIQUE(board, semantic_review_key))")
        # Releases are immutable capacity-audit records. The original reservation
        # remains queued so historical admission state is never rewritten.
        connection.execute("CREATE TABLE IF NOT EXISTS reviewer_admission_releases (reservation_id TEXT PRIMARY KEY REFERENCES reviewer_admissions(reservation_id), board TEXT NOT NULL, role TEXT NOT NULL, task_id TEXT NOT NULL, released_at INTEGER NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS reviewer_admission_provenance (reservation_id TEXT PRIMARY KEY REFERENCES reviewer_admissions(reservation_id), provenance_digest TEXT NOT NULL)")
        # F3 records are separate from caller-controlled Kanban fields. A production
        # writer is intentionally unavailable until PluginContext supports controller auth.
        connection.execute("CREATE TABLE IF NOT EXISTS controller_authority_records (record_id TEXT PRIMARY KEY, board TEXT NOT NULL, record_type TEXT NOT NULL CHECK(record_type IN ('accepted_findings_v1','review_budget_exception_v1')), writer_kind TEXT NOT NULL CHECK(writer_kind IN ('controller','worker')), controller_identity TEXT NOT NULL, record_digest TEXT NOT NULL, payload_json TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS controller_recovery_records (record_id TEXT PRIMARY KEY, board TEXT NOT NULL, root_key TEXT NOT NULL, writer_kind TEXT NOT NULL CHECK(writer_kind IN ('controller','worker')), controller_identity TEXT NOT NULL, failed_task_id TEXT NOT NULL, recovery_reason TEXT NOT NULL, artifact_ref TEXT NOT NULL, artifact_digest TEXT NOT NULL, record_digest TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL)")
        recovery_columns = {row[1] for row in connection.execute("PRAGMA table_info(controller_recovery_records)")}
        if "root_key" not in recovery_columns:
            # Existing records predate request binding and must never validate:
            # the nullable migration value fails the verifier below.
            connection.execute("ALTER TABLE controller_recovery_records ADD COLUMN root_key TEXT")
        connection.execute("CREATE TABLE IF NOT EXISTS router_producers (task_id TEXT PRIMARY KEY, board TEXT NOT NULL, created_at INTEGER NOT NULL)")
        return connection
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"reviewer admission ledger is unavailable or corrupt: {exc}") from exc


def _authority_record_digest(record: dict[str, Any]) -> str:
    return _canonical_json_digest({key: value for key, value in record.items() if key != "record_digest"})


def _validate_authority_record(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError("controller authority record must be an object")
    record_type = record.get("record_type")
    common = {"record_id", "board", "record_type", "controller_identity", "created_at", "expires_at"}
    accepted = common | {"prior_review_task_id", "prior_review_digest", "base_semantic_review_key", "semantic_review_key", "accepted_finding_ids"}
    budget = common | {"semantic_review_key", "packet_manifest_digest", "requested_token_count", "reason"}
    expected = accepted if record_type == "accepted_findings_v1" else budget if record_type == "review_budget_exception_v1" else None
    if expected is None or set(record) - {"record_digest"} != expected:
        raise ValueError("controller authority record has an invalid schema")
    for field in ("record_id", "board", "controller_identity"):
        if not isinstance(record[field], str) or not record[field].strip():
            raise ValueError(f"controller authority record {field} is malformed")
    if not all(isinstance(record[field], int) and record[field] >= 0 for field in ("created_at", "expires_at")) or record["expires_at"] <= record["created_at"]:
        raise ValueError("controller authority record expiry is malformed")
    if record_type == "accepted_findings_v1":
        _canonical_finding_ids(record["accepted_finding_ids"], field_name="accepted_finding_ids")
        for field in ("prior_review_digest", "base_semantic_review_key", "semantic_review_key"):
            if not isinstance(record[field], str) or not _SHA256_RE.fullmatch(record[field]):
                raise ValueError(f"controller authority record {field} is malformed")
    else:
        if not isinstance(record["semantic_review_key"], str) or not _SHA256_RE.fullmatch(record["semantic_review_key"]):
            raise ValueError("budget exception semantic_review_key is malformed")
        if not isinstance(record["packet_manifest_digest"], str) or not _SHA256_RE.fullmatch(record["packet_manifest_digest"]):
            raise ValueError("budget exception packet_manifest_digest is malformed")
        if not isinstance(record["requested_token_count"], int) or not 60_000 < record["requested_token_count"] <= 120_000:
            raise ValueError("budget exception requested_token_count must be in (60000,120000]")
        if not isinstance(record["reason"], str) or not record["reason"].strip():
            raise ValueError("budget exception reason is malformed")
    normalized = dict(record)
    normalized["record_digest"] = _authority_record_digest(record)
    return normalized


def _record_controller_authority_for_test(
    path: Path,
    record: dict[str, Any],
    *,
    trusted_context: object | None = None,
) -> None:
    """Test-only seeder gated by an opaque controller context.

    PluginContext exposes no controller-authenticated invocation contract, so
    production code deliberately has no authority-record writer.
    """
    if trusted_context is not _TEST_CONTROLLER_CONTEXT:
        raise ValueError("trusted controller test context is required")
    normalized = _validate_authority_record(record)
    connection = _ledger_connection(path)
    try:
        connection.execute("INSERT INTO controller_authority_records VALUES (?, ?, ?, 'controller', ?, ?, ?, ?, ?)", (
            normalized["record_id"], normalized["board"], normalized["record_type"], normalized["controller_identity"], normalized["record_digest"],
            json.dumps({key: value for key, value in normalized.items() if key != "record_digest"}, sort_keys=True), normalized["created_at"], normalized["expires_at"],
        ))
    finally:
        connection.close()


def _load_controller_authority(path: Path, record_id: str) -> dict[str, Any] | None:
    connection = _ledger_connection(path)
    try:
        row = connection.execute("SELECT board, record_type, writer_kind, controller_identity, record_digest, payload_json, created_at, expires_at FROM controller_authority_records WHERE record_id=?", (record_id,)).fetchone()
    finally:
        connection.close()
    if row is None or row[2] != "controller":
        return None
    try:
        record = json.loads(row[5])
        if not isinstance(record, dict) or record.get("record_id") != record_id:
            return None
        normalized = _validate_authority_record(record)
        if normalized["record_digest"] != row[4] or normalized["board"] != row[0] or normalized["record_type"] != row[1] or normalized["controller_identity"] != row[3] or normalized["created_at"] != row[6] or normalized["expires_at"] != row[7]:
            return None
        return normalized
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _controller_recovery_record_digest(record: dict[str, Any]) -> str:
    return _canonical_json_digest({key: value for key, value in record.items() if key != "record_digest"})


def _record_controller_recovery_for_test(
    path: Path, record: dict[str, Any], *, trusted_context: object | None = None,
) -> None:
    """Test-only recovery-record seeder; production has no caller-controlled writer."""
    if trusted_context is not _TEST_CONTROLLER_CONTEXT:
        raise ValueError("trusted controller test context is required")
    expected = {"record_id", "board", "root_key", "controller_identity", "failed_task_id", "recovery_reason",
                "artifact_ref", "artifact_digest", "created_at", "expires_at"}
    if not isinstance(record, dict) or set(record) != expected:
        raise ValueError("controller recovery record has an invalid schema")
    _validate_artifact_identity(record["artifact_ref"], record["artifact_digest"], field_prefix="controller recovery record")
    if not all(isinstance(record[field], str) and record[field].strip() for field in expected - {"created_at", "expires_at"}):
        raise ValueError("controller recovery record has malformed text fields")
    if not all(isinstance(record[field], int) and record[field] >= 0 for field in ("created_at", "expires_at")) or record["expires_at"] <= record["created_at"]:
        raise ValueError("controller recovery record expiry is malformed")
    normalized = dict(record)
    normalized["record_digest"] = _controller_recovery_record_digest(record)
    connection = _ledger_connection(path)
    try:
        connection.execute("INSERT INTO controller_recovery_records (record_id, board, root_key, writer_kind, controller_identity, failed_task_id, recovery_reason, artifact_ref, artifact_digest, record_digest, created_at, expires_at) VALUES (?, ?, ?, 'controller', ?, ?, ?, ?, ?, ?, ?, ?)", (
            normalized["record_id"], normalized["board"], normalized["root_key"], normalized["controller_identity"], normalized["failed_task_id"],
            normalized["recovery_reason"], normalized["artifact_ref"], normalized["artifact_digest"], normalized["record_digest"],
            normalized["created_at"], normalized["expires_at"],
        ))
    finally:
        connection.close()


def _verify_controller_recovery(path: Path, review: dict[str, Any], board: str, *, now: int | None = None) -> bool:
    recovery = review.get("controller_recovery")
    if not isinstance(recovery, dict):
        return False
    connection = _ledger_connection(path)
    try:
        row = connection.execute(
            "SELECT board, root_key, writer_kind, failed_task_id, recovery_reason, artifact_ref, artifact_digest, record_digest, created_at, expires_at FROM controller_recovery_records WHERE record_id=?",
            (recovery["authority_record_id"],),
        ).fetchone()
    finally:
        connection.close()
    if row is None or row[2] != "controller" or row[0] != board or row[1] != review.get("root_key") or row[9] <= (int(time.time()) if now is None else now):
        return False
    record = {"record_id": recovery["authority_record_id"], "board": row[0], "root_key": row[1], "controller_identity": "unavailable",
              "failed_task_id": row[3], "recovery_reason": row[4], "artifact_ref": row[5], "artifact_digest": row[6],
              "created_at": row[8], "expires_at": row[9]}
    # Controller identity is persisted but intentionally not supplied by the caller.
    connection = _ledger_connection(path)
    try:
        identity_row = connection.execute("SELECT controller_identity FROM controller_recovery_records WHERE record_id=?", (recovery["authority_record_id"],)).fetchone()
    finally:
        connection.close()
    if identity_row is None:
        return False
    record["controller_identity"] = identity_row[0]
    return (_controller_recovery_record_digest(record) == row[7]
            and recovery["failed_task_id"] == row[3]
            and recovery["recovery_reason"] == row[4]
            and str(Path(recovery["artifact_ref"]).resolve()) == str(Path(row[5]).resolve())
            and recovery["artifact_digest"] == row[6])


def _verify_re_review_authority(path: Path, review: dict[str, Any], board: str, *, now: int | None = None) -> bool:
    if review.get("controller_authority_marker") is not None:
        return False
    record_id = review.get("authority_record_id")
    record = _load_controller_authority(path, record_id) if isinstance(record_id, str) and record_id.strip() else None
    if record is None or record["record_type"] != "accepted_findings_v1" or record["board"] != board or record["expires_at"] <= (int(time.time()) if now is None else now):
        return False
    return (record["prior_review_digest"] == review["prior_review_digest"] and record["base_semantic_review_key"] == review["base_semantic_review_key"] and record["semantic_review_key"] == review["semantic_review_key"] and set(review["addressed_finding_ids"]).issubset(set(record["accepted_finding_ids"])))


def _verify_budget_exception(path: Path, record_id: str, board: str, semantic_review_key: str, packet_manifest_digest: str, requested_token_count: int, *, now: int | None = None) -> bool:
    record = _load_controller_authority(path, record_id)
    return bool(record and record["record_type"] == "review_budget_exception_v1" and record["board"] == board and record["expires_at"] > (int(time.time()) if now is None else now) and record["semantic_review_key"] == semantic_review_key and record["packet_manifest_digest"] == packet_manifest_digest and record["requested_token_count"] == requested_token_count)


def _review_provenance(review: dict[str, Any], reservation_id: str) -> dict[str, Any]:
    provenance = {
        "version": 2,
        "created_via": "ruoyu-cost-router-reviewer-pool-v2",
        "reservation_id": reservation_id,
        "root_key": review["root_key"],
        "risk": review["risk"],
        "stage": review["stage"],
        "role": review["role"],
        "exclusions": review["exclusions"],
        "prior_task_ids": review.get("parent_task_ids", []),
        "required_evidence_paths": review.get("required_evidence_paths", []),
        "final_authority": "controller",
    }
    if "producer_runs" in review:
        provenance["producer_runs"] = review["producer_runs"]
    elif "producer_profiles" in review:
        provenance["producer_profiles"] = review["producer_profiles"]
    elif "controller_recovery" in review:
        provenance["controller_recovery"] = review["controller_recovery"]
    if "artifact_ref" in review:
        provenance["artifact_ref"] = review["artifact_ref"]
    if "artifact_digest" in review:
        provenance["artifact_digest"] = review["artifact_digest"]
    if "semantic_review_key" in review:
        provenance["semantic_review_key"] = review["semantic_review_key"]
    return provenance


class _QueuedReservationLookupError(RuntimeError):
    """The host lookup was unavailable, so a queued reservation is ambiguous."""


_PROTECTED_REVIEW_MARKER = "Review provenance (protected router input):\n```json\n"
_PROTECTED_REVIEW_ENVELOPE = {
    "assignee": "worker-sol",
    "protected_review": True,
    "risk": "hard-L2",
    "stage": "protected_final",
    "version": 1,
}


def _protected_review_body_json(body: Any) -> str | None:
    """Extract legacy or host-enveloped protected provenance JSON, fail closed."""
    if not isinstance(body, str):
        return None
    if body.startswith(_PROTECTED_REVIEW_MARKER):
        return body[len(_PROTECTED_REVIEW_MARKER):]
    envelope_line, separator, remainder = body.partition("\n")
    expected = json.dumps(_PROTECTED_REVIEW_ENVELOPE, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    if not separator or envelope_line != expected or not remainder.startswith(_PROTECTED_REVIEW_MARKER):
        return None
    return remainder[len(_PROTECTED_REVIEW_MARKER):]


def _completed_reservation_matches(ctx, task_id: str, reservation_id: str, provenance_digest: str, board: str) -> bool:
    """Fail closed unless this exact queued reservation has freshly completed."""
    try:
        import tools.kanban_tools  # noqa: F401
        response = json.loads(ctx.dispatch_tool("kanban_show", {"task_id": task_id, "board": board}))
        task = response.get("task") if isinstance(response, dict) and isinstance(response.get("task"), dict) else None
        if not isinstance(task, dict) or task.get("id") != task_id or task.get("status") not in ("completed", "done"):
            return False
        body = task.get("body")
        encoded_body = _protected_review_body_json(body)
        if encoded_body is None:
            return False
        encoded, _, _ = encoded_body.partition("\n```\n")
        provenance = json.loads(encoded)
        return (
            provenance.get("reservation_id") == reservation_id
            and _canonical_json_digest(provenance) == provenance_digest
        )
    except Exception:
        return False


def _legacy_completed_reservation_provenance(
    ctx, task_id: str, reservation_id: str, board: str, role: str, root_key: str,
    stage: str, semantic_review_key: str | None,
) -> str | None:
    """Return a verified legacy provenance digest, or fail closed without a write."""
    try:
        import tools.kanban_tools  # noqa: F401
        response = json.loads(ctx.dispatch_tool("kanban_show", {"task_id": task_id, "board": board}))
        task = response.get("task") if isinstance(response, dict) and isinstance(response.get("task"), dict) else None
        if not isinstance(task, dict) or task.get("id") != task_id or task.get("status") not in ("completed", "done"):
            return None
        body = task.get("body")
        encoded_body = _protected_review_body_json(body)
        if encoded_body is None:
            return None
        encoded, terminator, _ = encoded_body.partition("\n```\n")
        if not terminator:
            return None
        provenance = json.loads(encoded)
        if not isinstance(provenance, dict):
            return None
        expected = {
            "reservation_id": reservation_id,
            "root_key": root_key,
            "stage": stage,
            "role": role,
        }
        if any(provenance.get(field) != value for field, value in expected.items()):
            return None
        if semantic_review_key is not None and provenance.get("semantic_review_key") != semantic_review_key:
            return None
        return _canonical_json_digest(provenance)
    except Exception:
        return None


def _reconcile_completed_reservations(ctx, connection: sqlite3.Connection, board: str, role: str) -> None:
    """Record idempotent releases only for fresh, matching completed host tasks."""
    rows = connection.execute(
        "SELECT admissions.reservation_id, admissions.task_id, admissions.root_key, admissions.stage, "
        "provenance.provenance_digest, semantics.semantic_review_key "
        "FROM reviewer_admissions AS admissions "
        "LEFT JOIN reviewer_admission_provenance AS provenance ON provenance.reservation_id=admissions.reservation_id "
        "LEFT JOIN reviewer_admission_semantics AS semantics ON semantics.reservation_id=admissions.reservation_id "
        "LEFT JOIN reviewer_admission_releases AS releases ON releases.reservation_id=admissions.reservation_id "
        "WHERE admissions.board=? AND admissions.role=? AND admissions.state='queued' AND releases.reservation_id IS NULL",
        (board, role),
    ).fetchall()
    for reservation_id, task_id, root_key, stage, provenance_digest, semantic_review_key in rows:
        if not isinstance(task_id, str) or not task_id:
            continue
        if provenance_digest is not None:
            matched = _completed_reservation_matches(ctx, task_id, reservation_id, provenance_digest, board)
        else:
            provenance_digest = _legacy_completed_reservation_provenance(
                ctx, task_id, reservation_id, board, role, root_key, stage, semantic_review_key,
            )
            matched = provenance_digest is not None
        if matched:
            if connection.execute("SELECT provenance_digest FROM reviewer_admission_provenance WHERE reservation_id=?", (reservation_id,)).fetchone() is None:
                connection.execute(
                    "INSERT INTO reviewer_admission_provenance VALUES (?, ?)",
                    (reservation_id, provenance_digest),
                )
            connection.execute(
                "INSERT OR IGNORE INTO reviewer_admission_releases VALUES (?, ?, ?, ?, ?)",
                (reservation_id, board, role, task_id, int(time.time())),
            )


def _queued_reservation_matches(ctx, task_id: str, review: dict[str, Any], board: str) -> bool:
    """Verify a queued reservation against the board-scoped host task before returning it."""
    try:
        import tools.kanban_tools  # noqa: F401
        response = json.loads(ctx.dispatch_tool("kanban_show", {"task_id": task_id, "board": board}))
        task = response.get("task") if isinstance(response, dict) and isinstance(response.get("task"), dict) else None
        # The explicit host request is board-scoped; kanban_show's task shape
        # intentionally does not expose task.board, so task_id remains the
        # object identity checked here.
        if not isinstance(task, dict) or task.get("id") != task_id:
            return False
        if task.get("status") not in {"ready", "running"}:
            return False
        body = task.get("body")
        marker = "Review provenance (protected router input):\n```json\n"
        if not isinstance(body, str) or not body.startswith(marker):
            return False
        encoded, _, _ = body[len(marker):].partition("\n```\n")
        provenance = json.loads(encoded)
        # Check provenance fields (v2 uses producer_runs, v1 uses producer_profiles)
        checks = ["root_key", "risk", "stage", "role", "exclusions", "semantic_review_key"]
        if all(provenance.get(name) == review[name] for name in checks if name in review):
            # F2 semantic reservations intentionally dedupe across independently
            # validated producer runs. Provenance validation happened before this
            # lookup and must not become part of the semantic identity.
            if "semantic_review_key" in review:
                return True
            # For producer_runs, check at least profile matches
            if "producer_runs" in provenance and "producer_runs" in review:
                return provenance["producer_runs"] == review["producer_runs"]
            if "producer_profiles" in provenance and "producer_profiles" in review:
                return provenance["producer_profiles"] == review["producer_profiles"]
            return True
        return False
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    except Exception as exc:
        raise _QueuedReservationLookupError("host reconciliation lookup failed") from exc


def _producer_run_matches(ctx, run: dict[str, Any], board: str, ledger: sqlite3.Connection) -> bool:
    """Bind v2 provenance to a ledger-recorded completed worker artifact."""
    try:
        registered = ledger.execute(
            "SELECT 1 FROM router_producers WHERE task_id=? AND board=?", (run["task_id"], board)
        ).fetchone()
        if registered is None:
            return False
        import tools.kanban_tools  # noqa: F401
        response = json.loads(ctx.dispatch_tool("kanban_show", {"task_id": run["task_id"], "board": board}))
        task = response.get("task") if isinstance(response, dict) and isinstance(response.get("task"), dict) else None
        if not isinstance(task, dict) or task.get("id") != run["task_id"]:
            return False
        if task.get("status") not in ("completed", "done") or task.get("assignee") != run["profile"]:
            return False
        if task.get("model_override") != run["model"] or task.get("provider_override") != run["provider"]:
            return False
        runs = response.get("runs")
        if not isinstance(runs, list) or not any(
            isinstance(item, dict)
            and item.get("id") == run["run_id"]
            and item.get("profile") == run["profile"]
            and item.get("status") == "completed"
            and item.get("outcome") == "completed"
            for item in runs
        ):
            return False
        artifact_path = str(Path(run["artifact_ref"]).resolve())
        events = response.get("events")
        if not isinstance(events, list):
            return False
        for event in events:
            if not isinstance(event, dict) or event.get("kind") != "completed" or event.get("run_id") != run["run_id"]:
                continue
            payload = event.get("payload")
            if isinstance(payload, str):
                payload = json.loads(payload)
            artifacts = (payload.get("metadata") or {}).get("artifacts", []) if isinstance(payload, dict) else []
            if any(str(Path(item).resolve()) == artifact_path for item in artifacts if isinstance(item, str)):
                return True
        return False
    except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
        return False
    except Exception:
        return False


def _validate_producer_runs(ctx, review: dict[str, Any], board: str, ledger: sqlite3.Connection) -> None:
    for run in review.get("producer_runs", []):
        if not _producer_run_matches(ctx, run, board, ledger):
            raise ValueError("producer_runs entry does not match a registered completed Kanban producer run/artifact")


def _admit_review(
    ctx,
    args: dict[str, Any],
    config: dict[str, Any],
    review: dict[str, Any],
    sol_admission: str,
) -> str:
    if review["role"] not in _REVIEWER_PROFILES:
        return _review_error("review role is unavailable")
    if review["role"] == "sol" and sol_admission != "sol_hard_l2":
        return _sol_admission_error("Sol card creation requires normalized sol_hard_l2 admission")
    if review["role"] == "sol":
        try:
            path = _ledger_path(config)
            if review.get("controller_recovery") is not None and not _verify_controller_recovery(path, review, args.get("board") or "default"):
                return _controller_handoff_error("controller recovery record is missing, invalid, expired, tampered, wrong-board, or does not bind this request")
            if review.get("review_kind") == "re_review" and not _verify_re_review_authority(path, review, args.get("board") or "default"):
                return _controller_handoff_error("re-review authority record is missing, invalid, expired, or does not bind this request")
            catalog = _route_catalog(config)
            if not catalog["sol"]["enabled"]:
                return _sol_admission_error("Sol route is disabled by operator configuration")
        except ValueError as exc:
            return _sol_admission_error(str(exc))
        # Atomic protected Sol review: only when semantic_review_key is available
        # (set by review_kind in _review_metadata). Without it, the caller
        # hasn't provided enough metadata for a valid packet, so fall back to
        # the existing deny_controller_handoff behavior.
        if "semantic_review_key" not in review:
            return _controller_handoff_error(
                "lossless Sol packet transport requires atomic task attachments before card visibility; public Kanban dispatch cannot provide that contract"
            )

        # Reserve, build packet, create task (blocked), attach evidence,
        # verify, then promote to queued. Any failure before the final
        # ledger transition leaves no visible/claimable Sol card.
        now = int(time.time())
        reservation_id = str(uuid.uuid4())
        connection = _ledger_connection(path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            if "semantic_review_key" in review:
                existing = connection.execute(
                    "SELECT admissions.reservation_id, admissions.task_id, admissions.state, admissions.updated_at FROM reviewer_admissions AS admissions JOIN reviewer_admission_semantics AS semantics ON semantics.reservation_id=admissions.reservation_id WHERE semantics.board=? AND semantics.semantic_review_key=?",
                    (args.get("board") or "default", review["semantic_review_key"]),
                ).fetchone()
            else:
                existing = connection.execute(
                    "SELECT reservation_id, task_id, state, updated_at FROM reviewer_admissions WHERE board=? AND root_key=? AND stage=?",
                    (args.get("board") or "default", review["root_key"], review["stage"]),
                ).fetchone()
            if existing:
                e_reservation_id, e_task_id, state, updated_at = existing
                stale = now - updated_at > 86_400
                try:
                    queued_match = (not stale and state == "queued" and isinstance(e_task_id, str) and e_task_id and _queued_reservation_matches(ctx, e_task_id, review, args.get("board") or "default"))
                except _QueuedReservationLookupError:
                    connection.execute("UPDATE reviewer_admissions SET state='quarantined', updated_at=? WHERE reservation_id=?", (int(time.time()), e_reservation_id))
                    connection.execute("COMMIT")
                    connection.close()
                    return _review_error("reviewer reservation host reconciliation failed and is quarantined; manual reconciliation required")
                if queued_match:
                    connection.execute("COMMIT")
                    connection.close()
                    return _safe_json({"routing_status": "existing", "admission_status": "existing", "task_id": e_task_id, "reservation_id": e_reservation_id, "controller_decision_required": True})
                connection.execute("UPDATE reviewer_admissions SET state='quarantined', updated_at=? WHERE reservation_id=?", (int(time.time()), e_reservation_id))
                connection.execute("COMMIT")
                connection.close()
                return _review_error("stale/mismatched reservation is quarantined")
            _reconcile_completed_reservations(ctx, connection, args.get("board") or "default", review["role"])
            count = connection.execute("SELECT COUNT(*) FROM reviewer_admissions AS admissions LEFT JOIN reviewer_admission_releases AS releases ON releases.reservation_id=admissions.reservation_id WHERE admissions.board=? AND admissions.role=? AND admissions.state IN ('reserved','queued','quarantined') AND releases.reservation_id IS NULL", (args.get("board") or "default", review["role"])).fetchone()[0]
            if count >= _REVIEWER_LIMITS[review["role"]]:
                connection.execute("COMMIT")
                connection.close()
                return _sol_admission_error(f"advisory {review['role']} review limit reached")
            connection.execute(
                "INSERT INTO reviewer_admissions VALUES (?, ?, ?, ?, ?, NULL, 'reserved', ?, ?)",
                (reservation_id, args.get("board") or "default", review["role"], review["root_key"], review["stage"], now, now),
            )
            if "semantic_review_key" in review:
                connection.execute(
                    "INSERT INTO reviewer_admission_semantics VALUES (?, ?, ?, ?)",
                    (reservation_id, args.get("board") or "default", review["semantic_review_key"], review["review_kind"]),
                )
            provenance = _review_provenance(review, reservation_id)
            connection.execute(
                "INSERT INTO reviewer_admission_provenance VALUES (?, ?)",
                (reservation_id, _canonical_json_digest(provenance)),
            )
            connection.execute("COMMIT")
        except sqlite3.Error as exc:
            connection.rollback()
            connection.close()
            return _review_error(f"ledger lock failure: {exc}")
        finally:
            connection.close()

        try:
            packet_info = _build_sol_review_packet_with_reservation(review, reservation_id, args, path)
            provenance = _review_provenance(review, reservation_id)
            body = "Review provenance (protected router input):\n```json\n" + json.dumps(provenance, sort_keys=True) + "\n```\n\n" + _prompt(args["goal"], args.get("context"), review["role"], "final_review")
            task_id = _create_atomic_protected_sol_task(
                title=_task_title(args["goal"], review["role"]), body=body,
                packet_info=packet_info, board=args.get("board"), priority=args.get("priority", 0),
            )
            # Protected-final cards remain blocked; do not promote or dispatch them.
            connection = _ledger_connection(path)
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE reviewer_admissions SET task_id=?, state='queued', updated_at=? WHERE reservation_id=? AND state='reserved'",
                    (task_id, int(time.time()), reservation_id),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise sqlite3.DatabaseError("reservation state changed unexpectedly")
                connection.execute("COMMIT")
            finally:
                connection.close()
            return _safe_json({
                "routing_status": "queued", "admission_status": "queued", "sol_admission": sol_admission,
                "task_id": task_id, "task_status": "blocked", "reservation_id": reservation_id,
                "review_provenance": provenance, "advisory_limit": _REVIEWER_LIMITS[review["role"]],
                "controller_decision_required": True,
                "non_guarantees": [
                    "Protected-final Sol cards remain blocked pending controller action.",
                    "Admission is limited to router-created cards recorded in this ledger.",
                    "This plugin does not enforce graph, scheduler, claim-time, global, manual-card, reassignment, or direct-DB behavior.",
                ],
            })
        except Exception as exc:
            try:
                connection = _ledger_connection(path)
                connection.execute("UPDATE reviewer_admissions SET state='quarantined', updated_at=? WHERE reservation_id=?", (int(time.time()), reservation_id))
                connection.execute("COMMIT")
                connection.close()
            except Exception:
                pass
            return _controller_handoff_error(f"Failed to create protected Sol review task: {exc}")
        finally:
            if 'packet_info' in locals():
                try:
                    import shutil
                    shutil.rmtree(packet_info["temp_dir"], ignore_errors=True)
                except Exception:
                    pass
    if review["role"] == "luna" and sol_admission != "luna_l1":
        return _sol_admission_error("Luna review card creation requires normalized luna_l1 admission")
    try:
        path = _ledger_path(config)
        if review.get("review_kind") == "re_review" and not _verify_re_review_authority(path, review, args.get("board") or "default"):
            return _controller_handoff_error("re-review authority record is missing, invalid, expired, or does not bind this request")
        catalog = _route_catalog(config)
        sol_route = catalog["sol"]
        max_cost, max_tokens, remaining = _budget_limits(args, config)
        input_tokens = max(1, len(_prompt(args["goal"], args.get("context"), "sol", "final_review")) // _CHARS_PER_TOKEN)
        if sol_admission == "sol_hard_l2":
            pinned_route = dict(sol_route, budget_fallbacks=[])
            try:
                _choose_budget_route("sol", pinned_route, catalog, input_tokens, max_cost, max_tokens, remaining)
                _authorize_route("sol", config, {**catalog, "sol": pinned_route})
            except ValueError as exc:
                return _sol_admission_error(str(exc))
        path = _ledger_path(config)
        if "producer_runs" in review:
            connection = _ledger_connection(path)
            try:
                _validate_producer_runs(ctx, review, args.get("board") or "default", connection)
            finally:
                connection.close()
        now = int(time.time())
        reservation_id = str(uuid.uuid4())
        connection = _ledger_connection(path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            if "semantic_review_key" in review:
                existing = connection.execute(
                    "SELECT admissions.reservation_id, admissions.task_id, admissions.state, admissions.updated_at FROM reviewer_admissions AS admissions JOIN reviewer_admission_semantics AS semantics ON semantics.reservation_id=admissions.reservation_id WHERE semantics.board=? AND semantics.semantic_review_key=?",
                    (args.get("board") or "default", review["semantic_review_key"]),
                ).fetchone()
            else:
                existing = connection.execute(
                    "SELECT reservation_id, task_id, state, updated_at FROM reviewer_admissions WHERE board=? AND root_key=? AND stage=?",
                    (args.get("board") or "default", review["root_key"], review["stage"]),
                ).fetchone()
            if existing:
                reservation_id, task_id, state, updated_at = existing
                stale = now - updated_at > 86_400
                try:
                    queued_match = (not stale and state == "queued" and isinstance(task_id, str) and task_id and _queued_reservation_matches(ctx, task_id, review, args.get("board") or "default"))
                except _QueuedReservationLookupError:
                    connection.execute("UPDATE reviewer_admissions SET state='quarantined', updated_at=? WHERE reservation_id=?", (int(time.time()), reservation_id))
                    connection.execute("COMMIT")
                    connection.close()
                    return _review_error("reviewer reservation host reconciliation failed and is quarantined; manual reconciliation is required")
                if queued_match:
                    connection.execute("COMMIT")
                    connection.close()
                    return _safe_json({"routing_status": "existing", "admission_status": "existing", "task_id": task_id, "reservation_id": reservation_id, "controller_decision_required": True})
                connection.execute("UPDATE reviewer_admissions SET state='quarantined', updated_at=? WHERE reservation_id=?", (int(time.time()), reservation_id))
                connection.execute("COMMIT")
                connection.close()
                return _review_error("stale, missing, mismatched, or invalid reviewer reservation is quarantined; manual reconciliation is required")
            _reconcile_completed_reservations(ctx, connection, args.get("board") or "default", review["role"])
            count = connection.execute("SELECT COUNT(*) FROM reviewer_admissions AS admissions LEFT JOIN reviewer_admission_releases AS releases ON releases.reservation_id=admissions.reservation_id WHERE admissions.board=? AND admissions.role=? AND admissions.state IN ('reserved','queued','quarantined') AND releases.reservation_id IS NULL", (args.get("board") or "default", review["role"])).fetchone()[0]
            if count >= _REVIEWER_LIMITS[review["role"]]:
                connection.execute("COMMIT")
                connection.close()
                if review["role"] == "sol":
                    return _sol_admission_error(f"advisory {review['role']} reviewer admission limit reached")
                return _review_error(f"advisory {review['role']} reviewer admission limit reached")
            connection.execute(
                "INSERT INTO reviewer_admissions VALUES (?, ?, ?, ?, ?, NULL, 'reserved', ?, ?)",
                (reservation_id, args.get("board") or "default", review["role"], review["root_key"], review["stage"], now, now),
            )
            if "semantic_review_key" in review:
                connection.execute(
                    "INSERT INTO reviewer_admission_semantics VALUES (?, ?, ?, ?)",
                    (reservation_id, args.get("board") or "default", review["semantic_review_key"], review["review_kind"]),
                )
            provenance = _review_provenance(review, reservation_id)
            connection.execute(
                "INSERT INTO reviewer_admission_provenance VALUES (?, ?)",
                (reservation_id, _canonical_json_digest(provenance)),
            )
            connection.execute("COMMIT")
        except sqlite3.Error as exc:
            connection.rollback()
            connection.close()
            return _review_error(f"reviewer admission ledger lock or transaction failure: {exc}")
        provenance = _review_provenance(review, reservation_id)
        body = "Review provenance (protected router input):\n```json\n" + json.dumps(provenance, sort_keys=True) + "\n```\n\n" + _prompt(args["goal"], args.get("context"), review["role"], "final_review")
        dedupe_key = review.get("semantic_review_key") or f"{review['root_key']}:{review['stage']}"
        task_args = {"title": _task_title(args["goal"], review["role"]), "body": body, "assignee": _REVIEWER_PROFILES[review["role"]], "board": args.get("board"), "parents": review.get("parent_task_ids", []), "workspace_kind": args.get("workspace_kind", "scratch"), "workspace_path": args.get("workspace_path"), "priority": args.get("priority", 0), "max_runtime_seconds": args.get("max_runtime_seconds", 900), "idempotency_key": f"{_PLUGIN_ID}:review:{args.get('board') or 'default'}:{dedupe_key}", "skills": args.get("skills", [])}
        try:
            import tools.kanban_tools  # noqa: F401
            result = json.loads(ctx.dispatch_tool("kanban_create", task_args))
            if not result.get("ok"):
                raise ValueError(result.get("error", "Kanban task creation failed"))
            connection.execute("BEGIN IMMEDIATE")
            update = connection.execute("UPDATE reviewer_admissions SET task_id=?, state='queued', updated_at=? WHERE reservation_id=? AND state='reserved'", (result["task_id"], int(time.time()), reservation_id))
            if update.rowcount != 1:
                raise sqlite3.DatabaseError("reservation state changed unexpectedly")
            connection.execute("COMMIT")
        except Exception as exc:
            try:
                connection.rollback()
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("UPDATE reviewer_admissions SET state='quarantined', updated_at=? WHERE reservation_id=?", (int(time.time()), reservation_id))
                connection.execute("COMMIT")
            except sqlite3.Error:
                pass
            finally:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass
            return _review_error(f"review task creation or persistence is ambiguous and quarantined: {exc}")
        finally:
            connection.close()
        return _safe_json({"routing_status": "queued" if result.get("created") else "existing", "admission_status": "queued", "sol_admission": sol_admission, "task_id": result["task_id"], "task_status": result.get("status"), "reservation_id": reservation_id, "review_provenance": provenance, "advisory_limit": _REVIEWER_LIMITS[review["role"]], "controller_decision_required": True, "non_guarantees": ["Admission is limited to router-created cards recorded in this ledger.", "This plugin does not enforce graph, scheduler, claim-time, global, manual-card, reassignment, or direct-DB behavior.", "Reservations are never released automatically; stale, corrupt, locked, and mismatched states require manual reconciliation."]})
    except ValueError as exc:
        return _review_error(str(exc))


def _handler(ctx, args: dict[str, Any], **_: Any) -> str:
    goal = args.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        return _error("ruoyu_cost_router requires a non-empty goal")
    context, route, task_type, project = args.get("context"), args.get("route"), args.get("task_type"), args.get("project")
    variant = args.get("variant")
    if context is not None and not isinstance(context, str):
        return _error("context must be a string when supplied")
    if route is not None and not isinstance(route, str):
        return _error("route must be a string when supplied")
    if variant is not None:
        return _error("variants were removed from the v5 flat catalog")
    if task_type is not None and not isinstance(task_type, str):
        return _error("task_type must be a string when supplied")
    if project is not None and not isinstance(project, dict):
        return _error("project must be an object when supplied")

    bounded_goal, goal_truncated = _bounded_text(_safe_text(goal), _MAX_GOAL_CHARS)
    bounded_context, context_truncated = _bounded_text(_safe_text(context or ""), _MAX_CONTEXT_CHARS)
    truncation = {"goal": goal_truncated, "context": context_truncated, "output": False}
    try:
        config = _plugin_config()
        try:
            review = _review_metadata(args)
        except ValueError as exc:
            return _review_error(str(exc))
        if isinstance(args.get("review"), dict) and args["review"].get("review_kind") == "re_review" and (not isinstance(args["review"].get("authority_record_id"), str) or not args["review"]["authority_record_id"].strip()):
            return _controller_handoff_error("re-review requires an immutable authority_record_id")
        if review is not None:
            try:
                sol_admission = _normalize_sol_admission(requested_tier="sol", review=review)
            except ValueError as exc:
                return _sol_admission_error(str(exc))
            return _admit_review(ctx, args, config, review, sol_admission)
        catalog = _route_catalog(config)
        routing = config.get("routing", {})
        if not isinstance(routing, dict):
            raise ValueError("routing must be an object when supplied")
        if task_type is not None and task_type not in _TASK_TYPE_TIERS:
            raise ValueError(f"unsupported task_type: {task_type}")
        requested_tier, selection_mode, matched_rule = _select_tier(route, bounded_goal, bounded_context, task_type, project, routing)
        try:
            sol_admission = _normalize_sol_admission(requested_tier=requested_tier, review=None)
        except ValueError as exc:
            return _sol_admission_error(str(exc))
        # The v5 catalog has no variants or budget fallbacks; selection and
        # authorization apply directly to the explicitly resolved route.
        route_entry = catalog[requested_tier]
        model_override = route_entry["model"]
        provider_override = route_entry["provider"]
        max_cost, max_tokens, remaining = _budget_limits(args, config)
        input_tokens = max(1, len(_prompt(bounded_goal, bounded_context, requested_tier, task_type)) // _CHARS_PER_TOKEN)
        tier, route_entry, estimate, budget, _ = _choose_budget_route(
            requested_tier, route_entry, catalog, input_tokens, max_cost, max_tokens, remaining
        )
        worker_profile = _authorize_route(tier, config, {**catalog, tier: route_entry})
        if worker_profile == _WORKER_PROFILES["sol"] and sol_admission != "sol_hard_l2":
            return _sol_admission_error("worker-sol requires normalized sol_hard_l2 admission before card creation")
        catalog[tier] = route_entry
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
            "model": model_override,
            "provider": provider_override,
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
        # The host's kanban_create response does not carry a `created` flag, so
        # derive queued-vs-existing from the task table itself: a fresh key
        # means we are the creating caller; an existing key means replay. This
        # lookup is best-effort: any failure degrades to "not pre-existing" and
        # must never block task creation.
        dedupe_key = task_args.get("idempotency_key")
        _pre_existing = False
        if dedupe_key:
            try:
                import hermes_cli.kanban_db as kanban_db
                _kconn = kanban_db.connect(board=args.get("board"))
                try:
                    _pre_existing = _kconn.execute(
                        "SELECT 1 FROM tasks WHERE idempotency_key = ? LIMIT 1",
                        (dedupe_key,),
                    ).fetchone() is not None
                finally:
                    _kconn.close()
            except Exception:
                _pre_existing = False
        task_result = json.loads(ctx.dispatch_tool("kanban_create", task_args))
        if not task_result.get("ok"):
            raise ValueError(task_result.get("error", "Kanban task creation failed"))
        # Persist plugin-owned producer identity outside the editable Kanban body.
        # A v2 review later requires this record in addition to host run/event proof.
        producer_ledger = _ledger_connection(_ledger_path(config))
        try:
            producer_ledger.execute("BEGIN IMMEDIATE")
            producer_ledger.execute(
                "INSERT OR IGNORE INTO router_producers (task_id, board, created_at) VALUES (?, ?, ?)",
                (task_result["task_id"], args.get("board") or "default", int(time.time())),
            )
            producer_ledger.execute("COMMIT")
        except sqlite3.Error as exc:
            producer_ledger.rollback()
            raise ValueError(f"router producer provenance persistence failed: {exc}") from exc
        finally:
            producer_ledger.close()
    except _BudgetExceededError as exc:
        rejected_tier = locals().get("tier") or locals().get("requested_tier")
        return _safe_json({
            "routing_status": "budget_exceeded",
            "controller_decision_required": True,
            "error": _safe_text(str(exc), limit=2_000),
            "tier": rejected_tier,
            "route": rejected_tier,
        })
    except ValueError as exc:
        rejected_tier = locals().get("tier") or locals().get("requested_tier")
        return _error(str(exc), tier=rejected_tier, route=rejected_tier)
    except Exception as exc:
        return _error(f"host-owned Kanban task creation failed: {exc}", tier=locals().get("tier"))

    task_id = task_result["task_id"]
    task_status = task_result.get("status")
    created = task_result.get("created")
    routing_status = "queued" if (created is True or (created is None and not _pre_existing)) else "existing"
    return _safe_json({
        "routing_status": routing_status,
        "task_id": task_id,
        "task_status": task_status,
        "board": args.get("board"),
        "tier": tier,
        "route": tier,
        "requested_tier": requested_tier,
        "worker_profile": worker_profile,
        "model_override": model_override,
        "provider_override": provider_override,
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
        "route": {"type": "string", "enum": list(_TIERS), "description": "Optional explicit v5 route."},
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
        "review": {"type": "object", "description": "Optional protected reviewer-pool admission metadata. Required fields: role, risk, stage, root_key, producer_runs (v2) or producer_profiles (v1 compatibility), exclusions. V2 producer runs require run_id, provider and verified artifact_ref/artifact_digest."},
    }, "required": ["goal"]},
}


def _create_atomic_protected_sol_task(*, title: str, body: str, packet_info: dict[str, Any],
                                      board: str | None, priority: int) -> str:
    """Use the host's all-or-nothing protected Sol creator; never public tools.

    Hermes >= 0.21 exposes ``kanban_db.create_protected_sol_review_task``;
    on older hosts (v0.20.x) fall back to ``create_task`` (blocked initial
    status, worker-sol assignee) plus ``store_attachment_bytes``, then run
    the same readback verification. The risk/stage semantics are preserved
    in the protected provenance body and the admission ledger regardless.
    """
    try:
        from hermes_cli import kanban_db
        creator = getattr(kanban_db, "create_protected_sol_review_task", None)
    except ImportError as exc:
        raise ValueError("host protected Sol atomic creator is unavailable") from exc

    def manifest_item(filename: str, path: Path) -> dict[str, Any]:
        data = path.read_bytes()
        return {
            "filename": filename,
            "data": data,
            "content_type": "application/json" if filename.endswith(".json") else "application/octet-stream",
            "sha256": hashlib.sha256(data).hexdigest(),
            "byte_count": len(data),
        }

    attachments = [manifest_item("packet.json", packet_info["packet_path"])]
    attachments.extend(
        manifest_item(f"source-{index}.json", source_path)
        for index, source_path in enumerate(packet_info["source_paths"])
    )
    conn = kanban_db.connect(board=board)
    try:
        if creator is not None:
            task_id = creator(
                conn, title=title, body=body, attachments=attachments, assignee="worker-sol",
                risk="hard-L2", stage="protected_final", priority=priority, board=board,
            )
        else:
            task_id = kanban_db.create_task(
                conn, title=title, body=body, assignee="worker-sol", created_by="router",
                initial_status="blocked", priority=priority, board=board,
            )
            for item in attachments:
                kanban_db.store_attachment_bytes(
                    conn, task_id, item["filename"], item["data"],
                    content_type=item["content_type"], board=board,
                )
        task = kanban_db.get_task(conn, task_id)
        stored = kanban_db.list_attachments(conn, task_id)
        if (task is None or task.assignee != "worker-sol" or task.status != "blocked"
                or len(stored) != len(attachments)):
            raise ValueError("atomic protected Sol creator readback verification failed")
        expected = {item["filename"]: (item["sha256"], item["byte_count"]) for item in attachments}
        for attachment in stored:
            data = Path(attachment.stored_path).read_bytes()
            digest, size = hashlib.sha256(data).hexdigest(), len(data)
            if attachment.filename not in expected or expected[attachment.filename] != (digest, size):
                raise ValueError("atomic protected Sol attachment verification failed")
        return task_id
    finally:
        conn.close()


def _build_sol_review_packet_with_reservation(review, reservation_id, args, ledger_path):
    """Build Sol review packet and source attachments using the given reservation_id."""
    import tempfile
    from pathlib import Path

    tmp_dir = Path(tempfile.mkdtemp(dir=ledger_path.parent))

    packet_content = _build_packet_content(review, reservation_id)
    packet_path = tmp_dir / "packet.json"
    packet_path.write_text(packet_content, encoding="utf-8")

    source_infos = []
    required_evidence_paths = review.get("required_evidence_paths", [])
    for i, source_path_str in enumerate(required_evidence_paths):
        source_path = Path(source_path_str)
        if not source_path.is_file():
            raise ValueError(f"required_evidence_paths[{i}] is not a regular file: {source_path_str}")
        source_infos.append({
            "logical_id": f"source-{i}",
            "attachment_id": f"source-{i}-att",
            "path": str(source_path.resolve()),
            "relevance": "required",
            "omission": "would-break-review",
        })

    input_tokens = _estimate_review_input_tokens(None)
    packet_metadata = {
        "version": _REVIEW_PACKET_MANIFEST_VERSION,
        "semantic_review_key": review.get("semantic_review_key") or _semantic_review_key(review.get("review_identity", {})),
        "packet": {"logical_id": "review-packet", "attachment_id": "packet-att", "path": str(packet_path)},
        "sources": source_infos,
        "input_tokens": input_tokens,
    }
    if review.get("authority_record_id"):
        packet_metadata["authority_record_id"] = review["authority_record_id"]

    prepared = _prepare_review_packet(packet_metadata)
    return {
        "packet_path": packet_path,
        "source_paths": [Path(info["path"]) for info in source_infos],
        "prepared_manifest": prepared["manifest"],
        "packet_sha256": prepared["packet"]["sha256"],
        "packet_bytes": prepared["packet"]["bytes"],
        "source_sha256s": [s["sha256"] for s in prepared["sources"]],
        "source_bytes": [s["bytes"] for s in prepared["sources"]],
        "temp_dir": tmp_dir,
        "prepared_packet": prepared,
    }


def _build_packet_content(review, reservation_id):
    """Build the content of the review packet file."""
    import json
    provenance = _review_provenance(review, reservation_id)
    return json.dumps(provenance, sort_keys=True, indent=2)


def _attach_review_packet(ctx, task_id, packet_info, board):
    """Attach packet and source files to the task."""
    import base64

    with open(packet_info["packet_path"], "rb") as f:
        packet_b64 = base64.b64encode(f.read()).decode("ascii")
    packet_result = json.loads(ctx.dispatch_tool("kanban_attach", {
        "task_id": task_id, "filename": "packet.json", "content_base64": packet_b64,
    }))
    if not packet_result.get("ok"):
        raise ValueError(f"Failed to attach packet: {packet_result.get('error')}")

    for i, source_path in enumerate(packet_info["source_paths"]):
        with open(source_path, "rb") as f:
            source_b64 = base64.b64encode(f.read()).decode("ascii")
        source_result = json.loads(ctx.dispatch_tool("kanban_attach", {
            "task_id": task_id, "filename": f"source-{i}.json", "content_base64": source_b64,
        }))
        if not source_result.get("ok"):
            raise ValueError(f"Failed to attach source {i}: {source_result.get('error')}")


def _verify_attached_review_packet(ctx, task_id, packet_info, board):
    """Verify that attached files match expected digests."""
    import hashlib

    attachments_result = json.loads(ctx.dispatch_tool("kanban_attachments", {"task_id": task_id}))
    if not attachments_result.get("ok"):
        raise ValueError(f"Failed to list attachments: {attachments_result.get('error')}")

    attachments = attachments_result.get("attachments", [])
    attachment_map = {att["filename"]: att for att in attachments}

    packet_att = attachment_map.get("packet.json")
    if not packet_att:
        raise ValueError("Packet attachment not found")
    packet_path_on_disk = packet_att.get("path")
    if not packet_path_on_disk:
        raise ValueError("Packet attachment path not available")

    with open(packet_path_on_disk, "rb") as f:
        packet_content = f.read()
    packet_sha256 = "sha256:" + hashlib.sha256(packet_content).hexdigest()
    if packet_sha256 != packet_info["packet_sha256"]:
        raise ValueError(f"Packet attachment digest mismatch: expected {packet_info['packet_sha256']}, got {packet_sha256}")
    if len(packet_content) != packet_info["packet_bytes"]:
        raise ValueError(f"Packet attachment size mismatch: expected {packet_info['packet_bytes']}, got {len(packet_content)}")

    for i, source_path in enumerate(packet_info["source_paths"]):
        source_att = attachment_map.get(f"source-{i}.json")
        if not source_att:
            raise ValueError(f"Source attachment {i} not found")
        source_path_on_disk = source_att.get("path")
        if not source_path_on_disk:
            raise ValueError(f"Source attachment {i} path not available")
        with open(source_path_on_disk, "rb") as f:
            source_content = f.read()
        source_sha256 = "sha256:" + hashlib.sha256(source_content).hexdigest()
        if source_sha256 != packet_info["source_sha256s"][i]:
            raise ValueError(f"Source {i} attachment digest mismatch: expected {packet_info['source_sha256s'][i]}, got {source_sha256}")
        if len(source_content) != packet_info["source_bytes"][i]:
            raise ValueError(f"Source {i} attachment size mismatch: expected {packet_info['source_bytes'][i]}, got {len(source_content)}")


def register(ctx) -> None:
    ctx.register_tool(name="ruoyu_cost_router", toolset="delegation", schema=_SCHEMA, handler=lambda args, **kwargs: _handler(ctx, args, **kwargs), emoji="R")
