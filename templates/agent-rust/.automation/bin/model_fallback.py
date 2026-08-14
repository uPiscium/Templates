#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path


class FallbackError(RuntimeError):
    pass


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def policy(root: Path) -> dict:
    path = root / ".automation" / "model-fallback.toml"
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FallbackError(f"missing fallback policy: {path}") from exc


def classify(text: str, status: int | None, cfg: dict) -> dict:
    lower = text.lower()
    non_fallback = cfg["classification"].get("non_fallback_markers", [])
    for marker in non_fallback:
        if marker.lower() in lower:
            return {"fallback": False, "reason": f"non-fallback marker: {marker}"}

    if status in cfg["classification"].get("retryable_http_status", []):
        return {"fallback": True, "reason": f"HTTP {status}"}

    for marker in cfg["classification"].get("usage_markers", []):
        if marker.lower() in lower:
            return {"fallback": True, "reason": marker}

    return {"fallback": False, "reason": "unclassified error"}


def role_chain(role: str, cfg: dict) -> dict:
    roles = cfg.get("roles", {})
    if role not in roles:
        raise FallbackError(f"unknown fallback role: {role}")
    entry = roles[role]
    agents = [entry["primary_agent"], *entry.get("fallback_agents", [])]
    models = [entry["primary_model"], *entry.get("fallback_models", [])]
    if len(agents) != len(models):
        raise FallbackError(f"invalid fallback chain for {role}: agent/model length mismatch")
    if len(set(models)) != len(models):
        raise FallbackError(f"invalid fallback chain for {role}: duplicate model")
    if agents[0] != role:
        raise FallbackError(f"invalid fallback chain for {role}: primary agent must match role")
    if any(agent != f"{role}-fallback" for agent in agents[1:]):
        raise FallbackError(f"invalid fallback chain for {role}: cross-role fallback agent")
    return {
        "role": role,
        "automatic": bool(entry.get("automatic", False)),
        "agents": agents,
        "models": models,
    }


def model_family(model: str, cfg: dict) -> str | None:
    matches = [family for family, models in cfg.get("families", {}).items() if model in models]
    return matches[0] if len(matches) == 1 else None


def recovery_route(role: str, unavailable_family: str, cfg: dict) -> dict:
    if unavailable_family not in cfg.get("families", {}):
        chain = role_chain(role, cfg)
        return {"status": "BLOCKED", "reason": "unknown model family", **chain}
    chain = role_chain(role, cfg)
    for agent, model in zip(chain["agents"], chain["models"]):
        family = model_family(model, cfg)
        if family is None:
            return {"status": "BLOCKED", "reason": "unknown model family", **chain}
        if family != unavailable_family:
            return {"status": "fallback" if agent != chain["agents"][0] else "primary",
                    "role": role, "agent": agent, "model": model,
                    "family": family, "reason": f"{unavailable_family} unavailable", **chain}
    return {"status": "BLOCKED", "reason": "fallback chain exhausted", **chain}


def agent_frontmatter(root: Path, agent: str) -> tuple[Path, str]:
    path = root / ".opencode" / "agents" / f"{agent}.md"
    if not path.is_file():
        raise FallbackError(f"missing agent definition for {agent}: {path}")
    text = path.read_text(encoding="utf-8")
    frontmatter = re.match(r"\A---\n(.*?)\n---(?:\n|\Z)", text, re.DOTALL)
    if not frontmatter:
        raise FallbackError(f"invalid agent frontmatter for {agent}: {path}")
    return path, frontmatter.group(1)


def agent_contract(root: Path, agent: str) -> dict:
    path, frontmatter = agent_frontmatter(root, agent)
    models = re.findall(r"(?m)^model:\s*(\S+)\s*$", frontmatter)
    if len(models) != 1:
        raise FallbackError(f"agent definition must declare exactly one model for {agent}: {path}")
    lines = frontmatter.splitlines()
    try:
        permission_index = lines.index("permission:")
    except ValueError as exc:
        raise FallbackError(f"agent definition is missing permission contract for {agent}: {path}") from exc
    permission_lines: list[str] = []
    for line in lines[permission_index + 1 :]:
        if line and not line[0].isspace():
            break
        permission_lines.append(line.rstrip())
    while permission_lines and not permission_lines[-1]:
        permission_lines.pop()
    if not permission_lines or any(line and not line.startswith("  ") for line in permission_lines):
        raise FallbackError(f"invalid permission contract for {agent}: {path}")
    return {"model": models[0], "permission": tuple(permission_lines)}


def ordered_contract(value):
    if isinstance(value, dict):
        return tuple((key, ordered_contract(item)) for key, item in value.items())
    if isinstance(value, list):
        return tuple(ordered_contract(item) for item in value)
    return value


def project_permission_contract(root: Path):
    path = root / "opencode.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FallbackError(f"missing OpenCode configuration: {path}") from exc
    except json.JSONDecodeError as exc:
        raise FallbackError(f"invalid OpenCode configuration: {path}") from exc
    if "permission" not in value:
        raise FallbackError(f"OpenCode configuration is missing permission contract: {path}")
    return ordered_contract(value["permission"])


def validate_agent_binding(role: str, cfg: dict, roots: list[Path]) -> None:
    chain = role_chain(role, cfg)
    root_contracts: list[dict[str, dict]] = []
    for agent, expected_model in zip(chain["agents"], chain["models"]):
        for root in roots:
            contract = agent_contract(root, agent)
            if contract["model"] != expected_model:
                raise FallbackError(
                    f"agent model mismatch for {agent}: policy={expected_model}, "
                    f"definition={contract['model']}, root={root}"
                )
    for root in roots:
        contracts = {agent: agent_contract(root, agent) for agent in chain["agents"]}
        primary_permission = contracts[chain["agents"][0]]["permission"]
        for agent in chain["agents"][1:]:
            if contracts[agent]["permission"] != primary_permission:
                raise FallbackError(f"role authority mismatch for {role}: {agent}, root={root}")
        root_contracts.append(contracts)
    for agent in chain["agents"]:
        expected_permission = root_contracts[0][agent]["permission"]
        for root, contracts in zip(roots[1:], root_contracts[1:]):
            if contracts[agent]["permission"] != expected_permission:
                raise FallbackError(f"cross-worktree authority mismatch for {agent}: root={root}")


def validate_project_permission_binding(roots: list[Path]) -> None:
    if len(roots) < 2:
        return
    expected = project_permission_contract(roots[0])
    for root in roots[1:]:
        if project_permission_contract(root) != expected:
            raise FallbackError(f"cross-worktree project permission mismatch: root={root}")


def next_fallback(role: str, failed_agent: str, cfg: dict) -> dict:
    chain = role_chain(role, cfg)
    if not chain["automatic"]:
        return {"available": False, "reason": "automatic fallback disabled", **chain}
    try:
        index = chain["agents"].index(failed_agent)
    except ValueError as exc:
        raise FallbackError(f"agent {failed_agent!r} is not in fallback chain for {role}") from exc
    next_index = index + 1
    if next_index >= len(chain["agents"]):
        return {"available": False, "reason": "fallback chain exhausted", **chain}
    return {
        "available": True,
        "agent": chain["agents"][next_index],
        "model": chain["models"][next_index],
        **chain,
    }


def append_evidence(root: Path, role: str, failed_model: str, fallback_model: str, reason: str, outcome: str) -> None:
    cfg = policy(root)
    chain = role_chain(role, cfg)
    if failed_model not in chain["models"] or fallback_model not in chain["models"]:
        raise FallbackError("fallback evidence models must belong to the configured role chain")
    failed_index = chain["models"].index(failed_model)
    if failed_index + 1 >= len(chain["models"]) or chain["models"][failed_index + 1] != fallback_model:
        raise FallbackError("fallback evidence models are not adjacent in the configured role chain")
    classified = classify(reason, None, cfg)
    if not classified["fallback"]:
        raise FallbackError("fallback evidence reason is not a classified usage-limit condition")
    reason_code = classified["reason"]
    state = root / ".task-state" / "task.md"
    if not state.is_file():
        raise FallbackError("Task State is required to record automatic fallback evidence")
    text = state.read_text(encoding="utf-8")
    heading = "### Model fallback"
    entry = (
        f"\n- Role: {role}; failed model: {failed_model}; fallback model: {fallback_model}; "
        f"reason: {reason_code}; outcome: {outcome}\n"
    )
    if heading in text:
        text = text.replace(heading, heading + entry, 1)
    elif "## Evidence" in text:
        text = text.replace("## Evidence", "## Evidence\n\n" + heading + entry, 1)
    else:
        text += "\n## Evidence\n\n" + heading + entry
    state.write_text(text, encoding="utf-8")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Role-scoped model fallback policy helper")
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("classify")
    c.add_argument("text")
    c.add_argument("--status", type=int)
    n = sub.add_parser("next")
    n.add_argument("role")
    n.add_argument("failed_agent")
    rr = sub.add_parser("route")
    rr.add_argument("role")
    rr.add_argument("family")
    r = sub.add_parser("record")
    r.add_argument("role")
    r.add_argument("failed_model")
    r.add_argument("fallback_model")
    r.add_argument("reason")
    r.add_argument("outcome", choices=["retrying", "succeeded", "failed", "blocked"])
    return p


def main() -> int:
    args = parser().parse_args()
    root = repository_root()
    cfg = policy(root)
    try:
        if args.command == "classify":
            print(json.dumps(classify(args.text, args.status, cfg)))
        elif args.command == "next":
            print(json.dumps(next_fallback(args.role, args.failed_agent, cfg)))
        elif args.command == "route":
            print(json.dumps(recovery_route(args.role, args.family, cfg)))
        elif args.command == "record":
            append_evidence(root, args.role, args.failed_model, args.fallback_model, args.reason, args.outcome)
            print(json.dumps({"recorded": True}))
    except FallbackError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
