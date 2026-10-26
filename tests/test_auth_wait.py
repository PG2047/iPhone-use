"""Automatic handoff checks must keep credentials and stale work inaccessible."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from iphone_use import Runtime, tool_result, VERSION
from wda_auth import AuthWaiter
from wda_client import WDAError
from test_controller import FakeWDA


class ProbeClient(FakeWDA):
    def __init__(self):
        super().__init__()
        self.compact = False
        self.blocker = False
        self.page = True
        self.empty = False
        self.rich = False
        self.fail_restore = False
        self.bulk_query_stalls = False
        self.visibility_first_stalls = False
        self.depth = 50
        self.stale_session = False
        self.session_failure = False
        self.on_query = lambda: None

    def close(self):
        pass

    def request(self, method, path, payload=None, timeout=None):
        if path == "/session":
            self.calls.append((method, path, payload))
            if self.session_failure:
                raise WDAError("pua_unreachable", "private backend text")
            self.stale_session = False
            return {"value": {"sessionId": "auth-probe-session"}}
        if path.endswith("/appium/settings"):
            self.calls.append((method, path, payload))
            if self.stale_session:
                raise WDAError("invalid session id", "private backend text")
            if method == "POST":
                value = payload["settings"]["shouldUseCompactResponses"]
                if not value and self.fail_restore:
                    raise WDAError("pua_timeout", "private backend text")
                self.compact = value
                self.depth = payload["settings"].get("snapshotMaxDepth", self.depth)
            return {"value": {"shouldUseCompactResponses": self.compact, "snapshotMaxDepth": self.depth, "snapshotMaxChildren": 2147483647}}
        if path.endswith(("/element", "/elements")):
            self.calls.append((method, path, payload))
            assert self.compact, "Query without compact responses"
            self.on_query()
            if self.bulk_query_stalls and path.endswith("/elements"):
                raise WDAError("action_uncertain", "bulk element identity serialization timed out", uncertain=True)
            predicate = payload["value"]
            if self.visibility_first_stalls and predicate.startswith("visible"):
                raise WDAError("action_uncertain", "unrelated element visibility timed out", uncertain=True)
            found = not self.empty if "TabBar" in predicate or "TypeIcon" in predicate else self.blocker if "Keyboard" in predicate else self.page
            if self.rich:
                return {"value": {"ELEMENT": "id", "value": "private input"} if path.endswith("/element") else [{"ELEMENT": "id", "value": "private input"}]}
            if path.endswith("/element"):
                if not found:
                    raise WDAError("no such element", "private backend text")
                return {"value": {"ELEMENT": "id"}}
            return {"value": [{"ELEMENT": "id"}] if found else []}
        if path not in ("/status", "/wda/locked", "/wda/activeAppInfo"):
            raise AssertionError("Forbidden auth request: " + path)
        return super().request(method, path, payload, timeout)


class AuthWaitTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = Runtime(directory.name)
        self.addCleanup(self.runtime.close)
        self.runtime.identify_device = Mock()
        self.client = ProbeClient()
        self.runtime.screen.set_paused(True)
        (self.runtime.state_dir / "session.json").write_text(json.dumps({"url": self.runtime.base_url, "session_id": "test-session"}))
        factory = patch("wda_auth.WDAClient", side_effect=lambda *a, **k: self.client)
        mock = factory.start()
        mock.remaining.side_effect = lambda deadline: max(.001, deadline-time.monotonic())
        self.addCleanup(factory.stop)

    def wait(self, **args):
        return self.runtime.call("pua_screen", {"action": "wait_auth", "bundle_id": self.client.app,
                                 "ready_label": "Account overview", "timeout": 2, **args})

    def test_phone_completion_resumes_without_dialog_or_user_answer(self):
        with patch("subprocess.run") as dialog:
            result = self.wait()
        self.assertTrue(result["resumed"])
        self.assertFalse(result["paused"])
        self.assertFalse(result["authentication_verified"])
        self.assertFalse(self.client.compact)
        dialog.assert_not_called()
        self.assertNotIn("nodes", result)
        self.assertNotIn("frame", result)
        self.assertEqual(sum(path == "/status" for _, path, _ in self.client.calls), 2)

    def test_auto_resume_when_bulk_identity_and_unfiltered_visibility_stall(self):
        # Real Settings page: /elements serializes many matched element IDs and
        # exceeds the deadline; existence needs only one matching element.
        self.client.bulk_query_stalls = True
        self.client.visibility_first_stalls = True
        result = self.wait()
        self.assertTrue(result.get("resumed"), result)
        self.assertFalse(result["paused"])
        self.assertFalse(self.client.compact)

    def test_legacy_resume_alias_uses_the_same_phone_evidence_without_dialog(self):
        with patch("subprocess.run") as dialog:
            result = self.wait(action="resume")
        dialog.assert_not_called()
        self.assertTrue(result["resumed"])
        self.assertFalse(result["confirmation_required"])
        self.assertEqual(result["runtime_version"], VERSION)

    def test_locked_wrong_app_blocker_missing_marker_and_empty_tree_keep_pause(self):
        for attr, value in (("locked", True), ("app", "other.app"), ("blocker", True), ("page", False), ("empty", True)):
            with self.subTest(attr=attr):
                old = getattr(self.client, attr)
                setattr(self.client, attr, value)
                result = self.wait(bundle_id="com.example.phone", timeout=1)
                self.assertEqual(result["state"], "waiting_auth")
                self.assertTrue(self.runtime.screen.paused())
                self.assertFalse(self.client.compact)
                setattr(self.client, attr, old)

    def test_restore_failure_and_noncompact_response_do_not_leak_or_resume(self):
        for attr in ("fail_restore", "rich"):
            with self.subTest(attr=attr):
                setattr(self.client, attr, True)
                wire = tool_result(self.runtime, {"name": "pua_screen", "arguments": {
                    "action": "wait_auth", "bundle_id": self.client.app, "ready_label": "Account overview", "timeout": 1}})
                self.assertNotIn("private", json.dumps(wire))
                self.assertTrue(self.runtime.screen.paused())
                setattr(self.client, attr, False)
                self.client.compact = False

    def test_new_pause_during_probe_cannot_be_cleared(self):
        self.client.on_query = lambda: self.runtime.screen.set_paused(True)
        result = self.wait()
        self.assertEqual(result["state"], "pause_changed")
        self.assertTrue(self.runtime.screen.paused())

    def test_device_unlock_needs_no_app_queries(self):
        self.runtime.screen.set_paused(False)
        self.runtime.screen.set_paused(True, reason="device_locked")
        result = self.runtime.call("pua_screen", {"action": "wait_auth", "timeout": 2})
        self.assertTrue(result["resumed"])
        self.assertEqual({p for _, p, _ in self.client.calls}, {"/status", "/wda/locked"})

    def test_orphan_pause_cannot_resume_in_arbitrary_app(self):
        result = self.runtime.call("pua_screen", {"action": "wait_auth", "timeout": 1})
        self.assertEqual(result["state"], "waiting_home_screen")
        self.assertTrue(result["paused"])
        self.assertFalse(any(p.endswith("/element") for _, p, _ in self.client.calls))

    def test_orphan_pause_recovers_only_on_unlocked_home_with_icons(self):
        self.client.app = "com.apple.springboard"
        for attr in ("locked", "empty", "blocker", "rich", "fail_restore"):
            with self.subTest(attr=attr):
                setattr(self.client, attr, True)
                result = self.runtime.call("pua_screen", {"action": "wait_auth", "timeout": 1})
                self.assertTrue(result["paused"])
                self.assertNotIn("private", json.dumps(result))
                setattr(self.client, attr, False)
        result = self.runtime.call("pua_screen", {"action": "wait_auth", "timeout": 2})
        self.assertEqual(result["state"], "home_screen_verified")
        self.assertFalse(result["paused"])
        self.assertFalse(result["authentication_verified"])

    def test_pause_saves_target_for_another_runtime(self):
        self.runtime.call("pua_screen", {"action": "pause", "bundle_id": self.client.app,
                                       "ready_label": "Account overview"})
        other = Runtime(self.runtime.state_dir)
        self.addCleanup(other.close)
        other.identify_device = Mock()
        result = other.call("pua_screen", {"action": "wait_auth", "timeout": 2})
        self.assertTrue(result["resumed"])

    def test_explicit_other_app_does_not_inherit_stored_marker(self):
        self.runtime.screen.set_paused(True, auth_target={"bundle_id": "original.app", "ready_label": "Overview"})
        self.client.app = "com.apple.springboard"
        result = self.runtime.call("pua_screen", {"action": "wait_auth", "bundle_id": "other.app", "timeout": 1})
        self.assertTrue(result["paused"])
        self.assertEqual(result["state"], "auth_target_mismatch")
        self.assertEqual(self.client.calls, [])

    def test_new_pause_discards_previous_target(self):
        self.runtime.screen.set_paused(True, auth_target={"bundle_id": self.client.app, "ready_label": "Overview"})
        self.runtime.screen.set_paused(True)
        self.assertEqual(self.runtime.screen._read_state()["auth_target"], {})

    def test_new_pause_during_home_probe_cannot_be_cleared(self):
        self.client.app = "com.apple.springboard"
        self.client.on_query = lambda: self.runtime.screen.set_paused(True)
        result = self.runtime.call("pua_screen", {"action": "wait_auth", "timeout": 2})
        self.assertTrue(result["paused"])
        self.assertEqual(result["state"], "pause_changed")

    def test_auth_heading_must_disappear_without_claiming_success(self):
        self.client.page = True
        result = self.wait(auth_label="Enter password", timeout=1)
        self.assertTrue(result["paused"])
        self.client.page = False
        result = self.runtime.call("pua_screen", {"action": "wait_auth", "bundle_id": self.client.app,
                                   "auth_label": "Enter password", "timeout": 2})
        self.assertTrue(result["resumed"])
        self.assertFalse(result["authentication_verified"])

    def test_stale_work_remains_blocked_after_automatic_resume(self):
        epoch = self.runtime.access.capture_epoch()
        self.assertTrue(self.wait()["resumed"])
        with self.assertRaises(WDAError) as error:
            self.runtime.call("pua_tap", {"x": 1, "y": 1}, received_epoch=epoch)
        self.assertEqual(error.exception.code, "access_paused")

    def test_session_from_other_endpoint_and_unhardened_service_stay_paused(self):
        policy = self.client.network_policy
        self.client.network_policy = {}
        self.assertEqual(self.wait()["state"], "probe_unavailable")
        self.assertTrue(self.runtime.screen.paused())
        self.assertFalse(any(p.endswith(("/element", "/elements")) for _, p, _ in self.client.calls))
        self.client.network_policy = policy
        (self.runtime.state_dir / "session.json").write_text(json.dumps({"url": "http://127.0.0.1:9999", "session_id": "old"}))
        self.assertEqual(self.wait()["error_code"], "auth_session_required")
        self.assertTrue(self.runtime.screen.paused())

    def test_shallow_snapshot_cannot_prove_auth_heading_disappeared(self):
        self.client.depth = 1
        result = self.wait(auth_label="Enter password")
        self.assertEqual(result["error_code"], "auth_snapshot_incomplete")
        self.assertTrue(self.runtime.screen.paused())
        self.assertFalse(any(p.endswith(("/element", "/elements")) for _, p, _ in self.client.calls))

    def test_missing_session_after_start_is_created_without_app_actions_or_content(self):
        (self.runtime.state_dir / "session.json").unlink()
        result = self.wait()
        self.assertTrue(result["resumed"])
        sessions = [payload for _, path, payload in self.client.calls if path == "/session"]
        self.assertEqual(len(sessions), 1)
        capabilities = sessions[0]["capabilities"]["alwaysMatch"]
        self.assertFalse(capabilities["shouldTerminateApp"])
        self.assertNotIn("bundleId", capabilities)
        self.assertNotIn("defaultAlertAction", capabilities)
        self.assertFalse(any("source" in path or "screenshot" in path or "launch" in path for _, path, _ in self.client.calls))
        self.assertFalse(self.client.compact)

    def test_stale_session_after_restart_is_replaced_once(self):
        self.client.stale_session = True
        result = self.wait()
        self.assertTrue(result["resumed"])
        self.assertEqual(sum(path == "/session" for _, path, _ in self.client.calls), 1)

    def test_missing_session_creation_failure_keeps_pause_and_suppresses_backend_text(self):
        (self.runtime.state_dir / "session.json").unlink()
        self.client.session_failure = True
        result = self.wait()
        self.assertTrue(result["paused"])
        self.assertEqual(result["state"], "probe_unavailable")
        self.assertNotIn("private", json.dumps(result))

    def test_missing_session_on_locked_or_wrong_app_does_not_create_session(self):
        (self.runtime.state_dir / "session.json").unlink()
        for args in ({"bundle_id": "other.app"}, {"bundle_id": self.client.app}):
            self.client.locked = args["bundle_id"] == self.client.app
            result = self.wait(timeout=1, **args)
            self.assertTrue(result["paused"])
        self.assertFalse(any(path == "/session" for _, path, _ in self.client.calls))

    def test_wait_does_not_start_another_probe_with_subsecond_budget(self):
        now = [0.0]
        def probe(*args):
            now[0] += 1.4
            return False
        with patch("wda_auth.time") as clock, patch.object(AuthWaiter, "_probe", side_effect=probe) as queried:
            clock.monotonic.side_effect = lambda: now[0]
            clock.sleep.side_effect = lambda duration: now.__setitem__(0, now[0] + duration)
            result = self.wait(timeout=3)
        queried.assert_called_once()
        self.assertEqual(result["state"], "waiting_auth")
        self.assertTrue(result["paused"])

    def test_budget_expiry_is_waiting_but_early_transport_timeout_is_error(self):
        for elapsed, expected in ((3.0, "waiting_auth"), (1.0, "probe_unavailable")):
            with self.subTest(elapsed=elapsed):
                now = [0.0]
                def probe(*args):
                    now[0] += elapsed
                    raise WDAError("pua_timeout", "private backend text")
                with patch("wda_auth.time") as clock, patch.object(AuthWaiter, "_probe", side_effect=probe):
                    clock.monotonic.side_effect = lambda: now[0]
                    result = self.wait(timeout=3)
                self.assertEqual(result["state"], expected)
                self.assertTrue(result["paused"])
                self.assertNotIn("private", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
