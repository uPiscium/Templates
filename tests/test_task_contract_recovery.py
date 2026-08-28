from __future__ import annotations

import importlib.util
import io
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BRIDGE_PATH = ROOT / "tools" / "automation_recovery_bridge.py"
spec = importlib.util.spec_from_file_location("recovery_bridge_test", BRIDGE_PATH)
assert spec and spec.loader
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)


class TaskContractRecoveryTest(unittest.TestCase):
    def test_parser_keeps_maintenance_commands_and_accepts_numeric_argument(self) -> None:
        self.assertEqual(
            bridge.parser().parse_args(["recover-maintenance-authority", "/tmp/task"]).command,
            "recover-maintenance-authority",
        )
        args = bridge.parser().parse_args(["commit-recovered-maintenance", "/tmp/task", "T-1"])
        self.assertEqual((args.command, args.task, args.message), ("commit-recovered-maintenance", "T-1", ""))
        args = bridge.parser().parse_args(["recover-task-contract-from-issue", "/tmp/task", "19"])
        self.assertEqual((args.command, args.issue), ("recover-task-contract-from-issue", "19"))

    def test_dirty_source_is_rejected_before_target_engine_call(self) -> None:
        with mock.patch.object(bridge, "_clean_root", side_effect=bridge.BridgeError("dirty")), \
             mock.patch.object(bridge, "_verify_bootstrap"), \
             mock.patch.object(bridge, "_verified_engine") as verified, \
             mock.patch.object(sys, "argv", ["bridge", "recover-task-contract-from-issue", "/tmp/task", "19"]), \
             mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(bridge.main(), 2)
        verified.assert_not_called()

    def test_tampered_bootstrap_is_rejected_before_target_changes(self) -> None:
        with mock.patch.object(bridge, "_clean_root", return_value="a" * 40), \
             mock.patch.object(bridge, "_verify_bootstrap", side_effect=bridge.BridgeError("tampered")), \
             mock.patch.object(bridge, "_verified_engine") as verified, \
             mock.patch.object(sys, "argv", ["bridge", "recover-task-contract-from-issue", "/tmp/task", "19"]), \
             mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(bridge.main(), 2)
        verified.assert_not_called()

    def test_verified_task_contract_is_loaded_from_head_blob(self) -> None:
        contract_bytes = bridge.CONTRACT_PATH.read_bytes()
        lifecycle_bytes = bridge.LIFECYCLE_PATH.read_bytes()

        def tree_blob(_root: Path, _revision: str, path: str):
            return ("contract", 0o100755) if path.endswith("task_contract.py") else ("lifecycle", 0o100644)

        def blob(_root: Path, oid: str) -> bytes:
            return contract_bytes if oid == "contract" else lifecycle_bytes

        with mock.patch.object(bridge, "_tree_blob", side_effect=tree_blob), \
             mock.patch.object(bridge, "_blob", side_effect=blob):
            with bridge._verified_task_contract(ROOT, "0" * 40) as contract:
                self.assertNotEqual(Path(contract.__file__).resolve(), bridge.CONTRACT_PATH.resolve())
                self.assertTrue(hasattr(contract, "recover_task_from_issue"))


if __name__ == "__main__":
    unittest.main()
