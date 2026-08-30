#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import agent_core
import automation_upgrade as upgrade
import publication_metadata as publication
import task_contract
import task_lifecycle as lifecycle


class MaintenanceError(RuntimeError):
    pass


def root() -> Path:
    installed = Path(__file__).resolve().parents[2]
    discovered = agent_core.repo_root(installed)
    if discovered != installed:
        raise MaintenanceError("installed maintenance lifecycle path does not match the Git worktree root")
    return installed


def _current_or_main_can_inspect(root_path: Path, record: lifecycle.WorktreeRecord) -> None:
    current = lifecycle.current_worktree(root_path)
    main = lifecycle.main_worktree(root_path)
    if current.path not in {main.path, record.path}:
        raise MaintenanceError(
            f"maintenance check cannot inspect sibling Task worktree {record.path} from {current.path}"
        )


def _stored_contract(root_path: Path, task: str) -> tuple[lifecycle.WorktreeRecord, dict]:
    record = lifecycle.worktree_for_task(root_path, task)
    _current_or_main_can_inspect(root_path, record)
    try:
        result = task_contract.validate_contract(record.path, task)
        if task_contract.repository_identity(record.path) != result["repository"]:
            raise MaintenanceError("live repository identity mismatch")
    except task_contract.ContractError as exc:
        raise MaintenanceError(str(exc)) from exc
    return record, result


def _validated_contract(root_path: Path, task: str) -> tuple[lifecycle.WorktreeRecord, dict]:
    record, result = _stored_contract(root_path, task)
    try:
        task_contract._validate_authoritative_issue(
            record.path, task, result["repository"], result["sha256"]
        )
    except task_contract.ContractError as exc:
        raise MaintenanceError(str(exc)) from exc
    return record, result


def _read_json_regular(path: Path, description: str) -> dict:
    if path.is_symlink() or not path.is_file():
        raise MaintenanceError(f"{description} is unavailable or unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MaintenanceError(f"{description} is invalid") from exc
    if not isinstance(value, dict):
        raise MaintenanceError(f"{description} is invalid")
    return value


def _validate_active_receipt(record: lifecycle.WorktreeRecord, task: str) -> dict:
    path = upgrade.receipt_path(record.path)
    try:
        receipt = upgrade.validate_receipt_schema(_read_json_regular(path, "active maintenance receipt"))
        upgrade.validate_authority(record.path, receipt)
    except upgrade.UpgradeError as exc:
        raise MaintenanceError(str(exc)) from exc
    if (
        receipt.get("status") != "active"
        or receipt.get("task_id") != task
        or receipt.get("branch") != record.branch
        or receipt.get("worktree") != str(record.path)
    ):
        raise MaintenanceError("active maintenance receipt identity mismatch")
    if upgrade.consumed_receipt_path(record.path).exists():
        raise MaintenanceError("active and consumed maintenance receipts coexist")
    head = agent_core.git("rev-parse", "HEAD", cwd=record.path)
    if receipt.get("authority_head") != head or record.head != head:
        raise MaintenanceError("active maintenance receipt authority HEAD is stale")
    try:
        paths = upgrade.receipt_paths(record.path, receipt)
        if upgrade.pending_paths(record.path) != paths:
            raise MaintenanceError("pending paths do not exactly match the active maintenance receipt")
        fingerprints = receipt.get("path_fingerprints")
        if not isinstance(fingerprints, dict) or set(fingerprints) != set(paths):
            raise MaintenanceError("active maintenance receipt fingerprints do not match its paths")
        if any(upgrade.file_fingerprint(record.path, item) != fingerprints[item] for item in paths):
            raise MaintenanceError("active maintenance receipt path fingerprint changed")
    except upgrade.UpgradeError as exc:
        raise MaintenanceError(str(exc)) from exc
    return receipt


def _validate_consumed_receipt(record: lifecycle.WorktreeRecord, task: str) -> dict:
    if upgrade.receipt_path(record.path).exists():
        raise MaintenanceError("active maintenance receipt still exists after commit")
    path = upgrade.consumed_receipt_path(record.path)
    value = _read_json_regular(path, "consumed maintenance receipt")
    required = set(upgrade.RECEIPT_FIELDS) | {"commit_sha"}
    if set(value) != required or value.get("schema_version") != 1 or value.get("status") != "consumed":
        raise MaintenanceError("consumed maintenance receipt has an invalid schema")
    if (
        value.get("task_id") != task
        or value.get("branch") != record.branch
        or value.get("worktree") != str(record.path)
    ):
        raise MaintenanceError("consumed maintenance receipt identity mismatch")
    commit = value.get("commit_sha")
    if not isinstance(commit, str):
        raise MaintenanceError("consumed maintenance receipt has no commit SHA")
    try:
        upgrade.validate_commit_oid(record.path, commit, field="maintenance commit")
        upgrade.validate_commit_oid(
            record.path, value.get("authority_head"), field="maintenance authority HEAD"
        )
    except upgrade.UpgradeError as exc:
        raise MaintenanceError(str(exc)) from exc
    local_head = agent_core.git("rev-parse", "HEAD", cwd=record.path)
    branch_head = agent_core.git(
        "rev-parse", "--verify", f"refs/heads/{record.branch}", cwd=record.path
    )
    if local_head != commit or branch_head != commit or record.head != commit:
        raise MaintenanceError("maintenance Task HEAD does not match the consumed receipt commit")
    parent = agent_core.git("rev-parse", f"{commit}^", cwd=record.path)
    if parent != value["authority_head"]:
        raise MaintenanceError("maintenance commit parent does not match the receipt authority HEAD")
    changed = sorted(
        line
        for line in agent_core.git(
            "diff-tree", "--no-commit-id", "--name-only", "-r", commit, cwd=record.path
        ).splitlines()
        if line
    )
    paths = value.get("changed_paths")
    if not isinstance(paths, list) or paths != sorted(set(paths)) or changed != paths:
        raise MaintenanceError("maintenance commit paths do not match the consumed receipt")
    fingerprints = value.get("path_fingerprints")
    if not isinstance(fingerprints, dict) or set(fingerprints) != set(paths):
        raise MaintenanceError("consumed maintenance receipt fingerprints do not match its paths")
    try:
        if any(upgrade.file_fingerprint(record.path, item) != fingerprints[item] for item in paths):
            raise MaintenanceError("committed maintenance path fingerprint changed")
    except upgrade.UpgradeError as exc:
        raise MaintenanceError(str(exc)) from exc
    if agent_core.git("status", "--porcelain", "--untracked-files=all", cwd=record.path):
        raise MaintenanceError("maintenance Task worktree is not clean after commit")
    return value


def _remote_head(record: lifecycle.WorktreeRecord) -> str | None:
    try:
        return lifecycle.remote_branch_head(record)
    except lifecycle.LifecycleError as exc:
        raise MaintenanceError(str(exc)) from exc


def _pr_evidence(record: lifecycle.WorktreeRecord, repository: str, commit: str) -> dict | None:
    pr = agent_core.pr_for_branch(record.path, record.branch or "", repository)
    if pr is None:
        return None
    expected = {
        "headRefName": record.branch,
        "baseRefName": agent_core.default_branch(record.path),
        "headRefOid": commit,
        "isCrossRepository": False,
    }
    mismatches = [name for name, wanted in expected.items() if pr.get(name) != wanted]
    if mismatches or not isinstance(pr.get("number"), int):
        raise MaintenanceError(
            "maintenance pull request identity is stale or inconsistent: "
            + ", ".join(mismatches or ["number"])
        )
    if pr.get("state") not in {"OPEN", "MERGED"}:
        raise MaintenanceError(f"maintenance pull request has unsupported state: {pr.get('state')}")
    return pr


def _maintenance_stage(record: lifecycle.WorktreeRecord, task: str, contract: dict) -> dict:
    status = lifecycle.state_status(lifecycle.state_path(record.path))
    if status == "merged":
        return {
            **contract,
            "status": "COMPLETED",
            "mode": "maintenance",
            "taskStatus": status,
            "stage": "merged",
        }
    if status != "initialized":
        raise MaintenanceError(
            f"maintenance lifecycle requires Task status initialized or merged; found {status}"
        )

    active = upgrade.receipt_path(record.path)
    consumed = upgrade.consumed_receipt_path(record.path)
    if active.exists():
        receipt = _validate_active_receipt(record, task)
        return {
            **contract,
            "status": "READY",
            "mode": "maintenance",
            "taskStatus": status,
            "stage": "applied",
            "sourceRevision": receipt["source_revision"],
        }
    if consumed.exists():
        receipt = _validate_consumed_receipt(record, task)
        commit = receipt["commit_sha"]
        remote = _remote_head(record)
        pr = _pr_evidence(record, contract["repository"], commit)
        stage = "committed"
        if remote is not None:
            if remote != commit:
                raise MaintenanceError("live remote Task branch does not match the maintenance commit")
            stage = "pushed"
        if pr is not None:
            if pr.get("state") == "MERGED":
                stage = "merged-remote"
            elif pr.get("isDraft") is True:
                stage = "draft-pr-created"
            elif pr.get("isDraft") is False:
                stage = "ready"
            else:
                raise MaintenanceError("maintenance pull request draft state is invalid")
        return {
            **contract,
            "status": "READY",
            "mode": "maintenance",
            "taskStatus": status,
            "stage": stage,
            "commit": commit,
            "sourceRevision": receipt["source_revision"],
            "remoteHead": remote,
            "pr": pr["number"] if pr is not None else None,
        }

    try:
        initial = task_contract.check_contract(record.path, task)
    except task_contract.ContractError as exc:
        raise MaintenanceError(
            "maintenance Task has no valid maintenance receipt and is no longer pristine: "
            + str(exc)
        ) from exc
    return {
        **initial,
        "status": "READY",
        "mode": "maintenance",
        "taskStatus": status,
        "stage": "pristine",
    }


def maintenance_check(root_path: Path, task: str) -> dict:
    record, contract = _validated_contract(root_path, task)
    return _maintenance_stage(record, task, contract)


def _completed_role(record: lifecycle.WorktreeRecord, task: str, role: str) -> bool:
    state = lifecycle.read_work_units(record, task)
    return any(
        isinstance(unit, dict)
        and unit.get("requested_role") == role
        and unit.get("state") == "completed"
        for unit in state.get("units", {}).values()
    )


def _require_review_evidence(record: lifecycle.WorktreeRecord, task: str) -> None:
    missing = [
        role
        for role in ("reviewer", "security-reviewer")
        if not _completed_role(record, task, role)
    ]
    if missing:
        raise MaintenanceError(
            "maintenance publication requires completed review evidence: " + ", ".join(missing)
        )


def maintenance_pr_create(root_path: Path, task: str) -> dict:
    record = lifecycle.require_local_task(root_path, task)
    ready = maintenance_check(root_path, task)
    if ready["stage"] not in {"pushed", "draft-pr-created"}:
        raise MaintenanceError(
            f"maintenance PR creation requires pushed or draft-pr-created stage; found {ready['stage']}"
        )
    receipt = _validate_consumed_receipt(record, task)
    commit = receipt["commit_sha"]
    remote = _remote_head(record)
    if remote != commit:
        raise MaintenanceError("maintenance PR creation requires the exact commit on the remote Task branch")
    _require_review_evidence(record, task)

    try:
        agent_core.verify(root_path, task)
        paths = agent_core.git(
            "diff", "--name-only", f"{agent_core._base_revision(root_path)}...{commit}", cwd=root_path
        ).splitlines()
        paths = sorted(line for line in paths if line)
        if paths != receipt["changed_paths"]:
            raise MaintenanceError("published maintenance diff does not match the consumed receipt")
        title, body_text = publication.canonical_metadata(
            root_path, task, head=commit, changed_paths=paths
        )
        publication.write_metadata(root_path, title, body_text)
        _, body_path, validated_body = agent_core._validated_local_metadata(root_path, task, commit)
    except (agent_core.AutomationError, publication.PublicationMetadataError) as exc:
        raise MaintenanceError(str(exc)) from exc

    repository = ready["repository"]
    branch = record.branch
    assert branch is not None
    base = agent_core.default_branch(root_path)
    existing = agent_core.pr_for_branch(root_path, branch, repository)
    if existing is None:
        agent_core.gh(
            "pr",
            "create",
            "--repo",
            repository,
            "--draft",
            "--base",
            base,
            "--head",
            branch,
            "--title",
            title,
            "--body-file",
            str(body_path),
            cwd=root_path,
        )
    if agent_core.canonical_repository(root_path).casefold() != repository.casefold():
        raise MaintenanceError("repository identity changed during maintenance pull request creation")
    pr = agent_core.pr_for_branch(root_path, branch, repository)
    if pr is None:
        raise MaintenanceError("created maintenance pull request cannot be re-read")
    try:
        agent_core._validate_live_pr(
            pr,
            branch=branch,
            base=base,
            head=commit,
            title=title,
            body=validated_body,
            draft=True,
        )
    except agent_core.AutomationError as exc:
        raise MaintenanceError(str(exc)) from exc
    return {
        "status": "DRAFT_PR_READY",
        "mode": "maintenance",
        "stage": "draft-pr-created",
        "task": task,
        "pr": pr["number"],
        "head": commit,
        "repository": repository,
    }


def _merged_pr(
    root_path: Path,
    record: lifecycle.WorktreeRecord,
    repository: str,
    pr_number: int,
    commit: str,
) -> dict:
    details = agent_core.pr_details(record.path, str(pr_number))
    expected = {
        "headRefName": record.branch,
        "baseRefName": lifecycle.default_branch(root_path),
        "headRefOid": commit,
        "isCrossRepository": False,
        "state": "MERGED",
    }
    mismatches = [name for name, wanted in expected.items() if details.get(name) != wanted]
    merge = details.get("mergeCommit")
    merge_oid = merge.get("oid") if isinstance(merge, dict) else None
    if (
        mismatches
        or details.get("number") != pr_number
        or not isinstance(merge_oid, str)
        or not re.fullmatch(r"[0-9a-fA-F]{40,64}", merge_oid)
    ):
        raise MaintenanceError(
            "merged maintenance pull request evidence is invalid: "
            + ", ".join(mismatches or ["number/mergeCommit"])
        )
    if agent_core.canonical_repository(record.path).casefold() != repository.casefold():
        raise MaintenanceError("maintenance repository identity changed")
    return {**details, "mergeCommitOid": merge_oid.lower()}


def _mark_maintenance_merged(record: lifecycle.WorktreeRecord, task: str) -> str:
    lifecycle.validate_task(task)
    lifecycle.require_resolved_contract(record, task)
    with lifecycle.work_units_lock(record):
        lifecycle.assert_task_identity(record, task)
        path = lifecycle.state_path(record.path)
        previous = lifecycle.state_status(path)
        if previous == "merged":
            return "already-finalized"
        if previous != "initialized":
            raise MaintenanceError(
                f"maintenance finalization requires initialized or merged; found {previous}"
            )
        text = path.read_text(encoding="utf-8")
        marker = "- Status: initialized"
        if text.count(marker) != 1:
            raise MaintenanceError("maintenance Task State status marker is ambiguous")
        lifecycle.atomic_text(path, text.replace(marker, "- Status: merged", 1))
    return "finalized"


def maintenance_finalize(root_path: Path, task: str, pr_number: int) -> dict:
    lifecycle.require_main_worktree(root_path)
    record, contract = _stored_contract(root_path, task)
    receipt = _validate_consumed_receipt(record, task)
    commit = receipt["commit_sha"]
    first = _merged_pr(root_path, record, contract["repository"], pr_number, commit)
    sync = lifecycle.synchronize_default_branch(root_path)
    merge_oid = first["mergeCommitOid"]
    if lifecycle.run(
        ["git", "merge-base", "--is-ancestor", merge_oid, sync["revision"]],
        cwd=root_path,
        check=False,
    ).returncode != 0:
        raise MaintenanceError(
            "maintenance PR merge commit is not present on the synchronized default branch"
        )
    lifecycle.require_synchronized_default_branch_revision(
        root_path, sync["branch"], sync["revision"]
    )
    _validate_consumed_receipt(record, task)
    second = _merged_pr(root_path, record, contract["repository"], pr_number, commit)
    if second["mergeCommitOid"] != merge_oid:
        raise MaintenanceError("maintenance pull request merge evidence changed during finalization")
    result = _mark_maintenance_merged(record, task)
    lifecycle.append_task_evidence(
        lifecycle.state_path(record.path),
        "### Maintenance publication",
        f"PR #{pr_number} merged from {commit}; merge commit {merge_oid}; finalization {result}",
    )
    return {
        "status": "FINALIZED",
        "mode": "maintenance",
        "stage": "merged",
        "task": task,
        "pr": pr_number,
        "publishedHead": commit,
        "mergeCommit": merge_oid,
        "defaultBranchRevision": sync["revision"],
        "transition": result,
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Guarded Automation Maintenance lifecycle")
    sub = value.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("task")
    create = sub.add_parser("pr-create")
    create.add_argument("task")
    finalize = sub.add_parser("finalize")
    finalize.add_argument("task")
    finalize.add_argument("pr", type=int)
    return value


def main() -> int:
    args = parser().parse_args()
    try:
        root_path = root()
        if args.command == "check":
            result = maintenance_check(root_path, args.task)
        elif args.command == "pr-create":
            result = maintenance_pr_create(root_path, args.task)
        elif args.command == "finalize":
            result = maintenance_finalize(root_path, args.task, args.pr)
        else:
            raise MaintenanceError(f"unsupported command: {args.command}")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (MaintenanceError, lifecycle.LifecycleError, upgrade.UpgradeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
