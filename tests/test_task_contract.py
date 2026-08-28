from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "components/agent-core/.automation/bin/task_contract.py"
spec = importlib.util.spec_from_file_location("task_contract_test", PATH)
assert spec and spec.loader
contract = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = contract
spec.loader.exec_module(contract)


def response(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")


class TaskContractTest(unittest.TestCase):
    def payload(self, **updates: object) -> dict:
        result = {
            "number": 19,
            "repository_url": "https://api.github.com/repos/acme/widgets",
            "html_url": "https://github.com/acme/widgets/issues/19",
            "title": "Dogfood title",
            "body": "Exact body with $HOME; do not interpret it\nsecond line",
            "state": "open",
            "labels": [],
            "assignees": [],
            "milestone": None,
        }
        result.update(updates)
        return result

    def test_identity_and_fetch_are_explicit_and_scrub_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = []

            def runner(command, **kwargs):
                fake.append((command, kwargs))
                return response(self.payload())

            with mock.patch.dict(os.environ, {"GH_REPO": "evil/x", "GH_HOST": "evil"}):
                with mock.patch.object(contract.lifecycle, "git", return_value="git@github.com:acme/widgets.git"):
                    identity, payload = contract.fetch_issue(root, "19", runner)
            self.assertEqual("acme/widgets", identity)
            self.assertEqual("Exact body with $HOME; do not interpret it\nsecond line", payload["body"])
            self.assertEqual(["gh", "api", "--hostname", "github.com", "repos/acme/widgets/issues/19"], fake[0][0])
            self.assertNotIn("GH_REPO", fake[0][1]["env"])
            self.assertNotIn("GH_HOST", fake[0][1]["env"])

    def test_rejects_pr_closed_wrong_urls_empty_and_control_title(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = mock.patch.object(contract.lifecycle, "git", return_value="https://github.com/acme/widgets")
            for changes, message in (
                ({"pull_request": {}}, "pull requests"),
                ({"state": "closed"}, "open"),
                ({"html_url": "https://github.com/other/widgets/issues/19"}, "URL"),
                ({"body": ""}, "body"),
                ({"title": "bad\ntext"}, "control"),
            ):
                with self.subTest(message=message), base, self.assertRaisesRegex(contract.ContractError, message):
                    contract.fetch_issue(root, "19", lambda *_args, **_kwargs: response(self.payload(**changes)))

    def test_numeric_validation_and_payload_filtering(self) -> None:
        with self.assertRaises(contract.ContractError):
            contract.validate_issue_number("019")
        source = self.payload(extra="must not persist")
        filtered = contract.authoritative_payload(source, 19, "acme/widgets")
        self.assertEqual("Exact body with $HOME; do not interpret it\nsecond line", filtered["body"])
        self.assertNotIn("extra", filtered)
        self.assertEqual(contract._digest(filtered), contract._digest(json.loads(json.dumps(filtered))))

    def write_pristine(self, root: Path, task: str = "19") -> contract.lifecycle.WorktreeRecord:
        branch = f"task/{task}-agent-core-v3-1-1"
        template = (ROOT / "components/agent-core/.automation/templates/task-state.md").read_text(
            encoding="utf-8"
        )
        automation_template = root / ".automation/templates/task-state.md"
        automation_template.parent.mkdir(parents=True)
        automation_template.write_text(template, encoding="utf-8")
        state = root / ".task-state/task.md"
        state.parent.mkdir(parents=True)
        values = {
            "@@TASK_ID@@": task,
            "@@BRANCH@@": branch,
            "@@WORKTREE@@": str(root),
            "@@BASE_BRANCH@@": "main",
            "@@BASE_REVISION@@": "a" * 40,
        }
        for marker, value in values.items():
            template = template.replace(marker, value)
        state.write_text(template, encoding="utf-8")
        return contract.lifecycle.WorktreeRecord(root, branch, "a" * 40)

    def hydration_patches(self, root: Path, record, *, dirty: str = "", head: str | None = None, units=None):
        def git(*args, **_kwargs):
            if args[0] == "status":
                return dirty
            if args[0] == "rev-parse":
                return head or "a" * 40
            raise AssertionError(args)

        return (
            mock.patch.object(contract.lifecycle, "require_local_task", return_value=record),
            mock.patch.object(contract.lifecycle, "read_work_units", return_value={"units": units or {}}),
            mock.patch.object(contract.lifecycle, "git", side_effect=git),
        )

    def test_dogfood_contract_hydrates_without_semantic_interpretation_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            record = self.write_pristine(root)
            payload = self.payload(
                title="Upgrade Agent Core to v3.1.1",
                body="## Purpose\nUpgrade safely.\n## Scope\nCore only.\n## Safety\nNo manual state edits.\n## Acceptance\nStrict init passes.",
            )
            patches = self.hydration_patches(root, record)
            with patches[0], patches[1], patches[2]:
                first = contract.hydrate_task_contract(root, "19", "19", payload, "acme/widgets")
                before = (root / ".task-state/task.md").read_bytes()
                second = contract.hydrate_task_contract(root, "19", "19", payload, "acme/widgets")
                ready = contract.validate_contract(root, "19")
            self.assertEqual(first, second)
            self.assertEqual(before, (root / ".task-state/task.md").read_bytes())
            self.assertEqual("READY", ready["status"])
            snapshot = json.loads((root / ".task-state/issue.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["body"], snapshot["payload"]["body"])
            self.assertNotIn("TBD", (root / ".task-state/task.md").read_text(encoding="utf-8"))

    def test_hydration_rejects_modified_progressed_dirty_work_unit_and_identity_mismatch(self) -> None:
        cases = ("modified", "progressed", "dirty", "unit", "identity")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                record = self.write_pristine(root, "20" if case == "identity" else "19")
                state = root / ".task-state/task.md"
                if case == "modified":
                    state.write_text(state.read_text(encoding="utf-8").replace("TBD", "changed", 1), encoding="utf-8")
                if case == "progressed":
                    state.write_text(state.read_text(encoding="utf-8").replace("Status: initialized", "Status: planning"), encoding="utf-8")
                patches = self.hydration_patches(
                    root,
                    record,
                    dirty="?? product.txt" if case == "dirty" else "",
                    units={"WU-19-01": {}} if case == "unit" else None,
                )
                with patches[0], patches[1], patches[2], self.assertRaises(contract.ContractError):
                    contract.hydrate_task_contract(root, record.branch.split("/")[1].split("-")[0], "19", self.payload(), "acme/widgets")

    def test_conflicting_retry_and_failed_write_preserve_prior_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            record = self.write_pristine(root)
            patches = self.hydration_patches(root, record)
            with patches[0], patches[1], patches[2]:
                contract.hydrate_task_contract(root, "19", "19", self.payload(), "acme/widgets")
                before = {path: path.read_bytes() for path in (root / ".task-state").iterdir()}
                with self.assertRaisesRegex(contract.ContractError, "content conflict"):
                    contract.hydrate_task_contract(root, "19", "19", self.payload(body="changed"), "acme/widgets")
            self.assertEqual(before, {path: path.read_bytes() for path in (root / ".task-state").iterdir()})

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            record = self.write_pristine(root)
            original = (root / ".task-state/task.md").read_bytes()
            real_state_write = contract._write_state_file
            patches = self.hydration_patches(root, record)
            injected = False

            def fail_state(directory_fd: int, name: str, content: bytes) -> None:
                nonlocal injected
                if name == "task.md" and not injected:
                    injected = True
                    raise OSError("injected")
                real_state_write(directory_fd, name, content)

            with patches[0], patches[1], patches[2], mock.patch.object(
                contract, "_write_state_file", side_effect=fail_state
            ), self.assertRaises(OSError):
                contract.hydrate_task_contract(root, "19", "19", self.payload(), "acme/widgets")
            self.assertEqual(original, (root / ".task-state/task.md").read_bytes())
            self.assertFalse((root / ".task-state/issue.json").exists())
            self.assertFalse((root / ".task-state/contract.json").exists())

    def test_validation_rejects_forged_payload_identity_even_with_recomputed_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            record = self.write_pristine(root)
            patches = self.hydration_patches(root, record)
            with patches[0], patches[1], patches[2]:
                contract.hydrate_task_contract(root, "19", "19", self.payload(), "acme/widgets")
                snapshot_path = root / ".task-state/issue.json"
                snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
                snapshot["payload"]["number"] = 20
                snapshot["sha256"] = contract._digest(snapshot["payload"])
                snapshot_path.write_text(json.dumps(snapshot, sort_keys=True) + "\n", encoding="utf-8")
                metadata_path = root / ".task-state/contract.json"
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                metadata["sha256"] = snapshot["sha256"]
                metadata_path.write_text(json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8")
                state_path = root / ".task-state/task.md"
                state_path.write_text(
                    state_path.read_text(encoding="utf-8").replace(
                        re.search(r"sha256=([0-9a-f]{64})", state_path.read_text(encoding="utf-8")).group(1),
                        snapshot["sha256"],
                    ),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(contract.ContractError, "identity or content"):
                    contract.validate_contract(root, "19")

    def test_directory_replacement_at_publication_boundary_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            record = self.write_pristine(root)
            original = (root / ".task-state/task.md").read_bytes()
            real_state_write = contract._write_state_file
            replaced = False
            patches = self.hydration_patches(root, record)

            def replace_after_final_write(directory_fd: int, name: str, content: bytes) -> None:
                nonlocal replaced
                real_state_write(directory_fd, name, content)
                if name == "task.md" and not replaced:
                    replaced = True
                    (root / ".task-state").rename(root / ".task-state-pinned")
                    (root / ".task-state").mkdir()

            with patches[0], patches[1], patches[2], mock.patch.object(
                contract, "_write_state_file", side_effect=replace_after_final_write
            ), self.assertRaisesRegex(contract.ContractError, "changed during hydration"):
                contract.hydrate_task_contract(root, "19", "19", self.payload(), "acme/widgets")
            self.assertEqual(original, (root / ".task-state-pinned/task.md").read_bytes())
            self.assertFalse((root / ".task-state/issue.json").exists())


if __name__ == "__main__":
    unittest.main()
