"""Bounded authentication handoff checks. Never fetch pixels or field values."""
import fcntl
import json
import os
import re
import tempfile
import time

from wda_client import WDAClient, WDAError, SESSION_SETTINGS
from wda_setup import network_hardened


class AuthWaiter:
    def __init__(self, runtime):
        self.runtime = runtime
        self.stage = "not_started"

    def wait(self, bundle_id=None, ready_label=None, auth_label=None, timeout=25):
        screen = self.runtime.screen
        state = screen._read_state()
        if not state["paused"]:
            return {"paused": False, "state": "not_paused", "fresh_observation_required": True}
        pause_id, epoch = state["pause_id"], state["access_epoch"]
        if not pause_id:
            # Give legacy pauses an identity without clearing them.
            pause_id = screen.reconnect_pause_id()
        device_lock = state["pause_reason"] == "device_locked"
        target = state.get("auth_target", {})
        if not device_lock and bundle_id and target.get("bundle_id") and bundle_id != target["bundle_id"]:
            return {"paused": True, "state": "auth_target_mismatch", "replay_action": False,
                    "next_step": "Use wait_auth without App parameters to reuse this pause's saved recovery target."}
        # Explicit targets never inherit labels from a different App.
        if not bundle_id or bundle_id == target.get("bundle_id"):
            bundle_id = bundle_id or target.get("bundle_id")
            ready_label = ready_label or target.get("ready_label")
            auth_label = auth_label or target.get("auth_label")
        home_only = not device_lock and not (bundle_id and (ready_label or auth_label))
        if home_only:
            bundle_id = "com.apple.springboard"
            ready_label = auth_label = None
        deadline = time.monotonic() + timeout
        stable = 0
        attempts = 0
        last_state = "waiting_home_screen" if home_only else "waiting_auth"
        budget_exhausted = False
        while time.monotonic() < deadline:
            # Do not begin a new round with a sub-second remainder after an
            # ordinary waiting result. It creates artificial transport timeouts.
            current = screen._read_state()
            if current["pause_id"] != pause_id or current["access_epoch"] != epoch:
                return {"paused": current["paused"], "state": "pause_changed", "fresh_observation_required": True}
            if attempts and stable == 0 and deadline - time.monotonic() < 1:
                break
            attempts += 1
            try:
                clear = self._probe(deadline, device_lock, bundle_id, ready_label, auth_label, home_only)
                stable = stable + 1 if clear else 0
                last_state = "waiting_home_screen" if home_only else "waiting_auth"
            except (WDAError, OSError, ValueError, TypeError, KeyError) as exc:
                if isinstance(exc, WDAError) and exc.code == "pua_timeout" and time.monotonic() >= deadline:
                    budget_exhausted = True
                    break
                # Backend error messages can contain page content. Return only a code.
                return {"paused": True, "state": "probe_unavailable",
                        "error_code": exc.code if isinstance(exc, WDAError) else "invalid_probe",
                        "probe_stage": self.stage,
                        "replay_action": False}
            if stable >= 2:
                resumed = screen.resume_after_auth_probe(pause_id, epoch)
                return {"paused": screen.paused(), "state": ("home_screen_verified" if home_only else "auth_ui_cleared") if resumed else "pause_changed",
                        "resumed": resumed, "authentication_verified": False,
                        "fresh_observation_required": True, "replay_action": False}
            time.sleep(min(1, max(0, deadline - time.monotonic())))
        return {"paused": True, "state": last_state, "retry_after_seconds": 1,
                "probe_budget_exhausted": budget_exhausted,
                "next_step": ("Recovery markers are missing. Return to the iPhone Home Screen; continue wait_auth to verify unlocked SpringBoard with visible app icons and no authentication UI. No Mac confirmation or chat reply is needed."
                              if home_only else "Continue wait_auth while the user authenticates on the phone. No Mac confirmation or chat reply is needed.")}

    def _probe(self, deadline, device_lock, bundle_id, ready_label, auth_label, home_only=False):
        runtime = self.runtime
        # Serialize settings with every normal phone operation, across processes.
        with (runtime.state_dir / "operation.lock").open("a") as lock:
            os.fchmod(lock.fileno(), 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            client = WDAClient(runtime.base_url, timeout=10)
            def request(method, path, payload=None):
                self.stage = path.rsplit("/", 1)[-1]
                return client.request(method, path, payload, timeout=min(10, WDAClient.remaining(deadline))).get("value")
            try:
                status = request("GET", "/status")
                config = runtime.setup_manager.config
                if not isinstance(status, dict) or status.get("ready") is not True or not network_hardened(status, config.get("device_port", 8100)):
                    raise WDAError("not_ready", "Authentication probe unavailable.")
                if request("GET", "/wda/locked") is not False:
                    return False
                if device_lock:
                    return True
                if (request("GET", "/wda/activeAppInfo") or {}).get("bundleId") != bundle_id:
                    return False
                cache_path = runtime.state_dir / "session.json"
                try:
                    cached = json.loads(cache_path.read_text())
                except FileNotFoundError:
                    cached = None
                if cached is not None and (not isinstance(cached, dict) or cached.get("url") != runtime.base_url):
                    raise WDAError("auth_session_required", "A matching local session is required.")
                cached = cached or {}
                sid = cached.get("session_id")
                if sid is not None and (not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", sid)):
                    raise WDAError("auth_session_required", "Invalid local session identity.")

                def create_probe_session():
                    # No bundleId, app launch, alert acceptance, source or pixels.
                    self.stage = "session"
                    result = client.request("POST", "/session", {"capabilities": {"alwaysMatch": {
                        "shouldWaitForQuiescence": False, "shouldTerminateApp": False,
                        "waitForIdleTimeout": 0}}}, timeout=min(10, WDAClient.remaining(deadline)))
                    value = result.get("value") or {}
                    new_sid = result.get("sessionId") or (value.get("sessionId") if isinstance(value, dict) else None)
                    if not isinstance(new_sid, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", new_sid):
                        raise WDAError("invalid_response", "Invalid authentication session identity.")
                    # Save before settings: later probes can retry settings without
                    # repeatedly creating sessions after a transient failure.
                    fd, temporary = tempfile.mkstemp(prefix=".auth-session-", dir=runtime.state_dir)
                    try:
                        with os.fdopen(fd, "w") as stream:
                            json.dump({"url": runtime.base_url, "session_id": new_sid,
                                       "auth_settings_pending": True}, stream)
                        os.replace(temporary, cache_path)
                    finally:
                        if os.path.exists(temporary):os.unlink(temporary)
                    return new_sid

                new_session = not sid
                if new_session:
                    sid = create_probe_session()
                prefix = f"/session/{sid}"
                try:
                    settings = request("GET", prefix + "/appium/settings")
                except WDAError as exc:
                    if new_session or exc.code != "invalid session id":raise
                    sid = create_probe_session()
                    new_session = True
                    prefix = f"/session/{sid}"
                    settings = request("GET", prefix + "/appium/settings")
                if new_session or cached.get("auth_settings_pending") is True:
                    request("POST", prefix + "/appium/settings", {"settings": {
                        **SESSION_SETTINGS, "shouldUseCompactResponses": True}})
                    settings = request("GET", prefix + "/appium/settings")
                compact = settings.get("shouldUseCompactResponses") if isinstance(settings, dict) else None
                if not isinstance(compact, bool):
                    raise WDAError("invalid_response", "Compact response mode is unavailable.")
                if settings.get("snapshotMaxDepth", 0) < 50 or settings.get("snapshotMaxChildren", 0) < 2147483647:
                    raise WDAError("auth_snapshot_incomplete", "Restore normal snapshot settings before checking authentication.")
                try:
                    request("POST", prefix + "/appium/settings", {"settings": {"shouldUseCompactResponses": True}})
                    if request("GET", prefix + "/appium/settings").get("shouldUseCompactResponses") is not True:
                        raise WDAError("invalid_response", "Compact response mode was not applied.")
                    def exists(predicate):
                        # /elements resolves and caches every match, which can stall
                        # on long or changing pages. Existence needs one ID only.
                        try:
                            value = request("POST", prefix + "/element", {"using": "predicate string", "value": predicate})
                        except WDAError as exc:
                            if exc.code == "no such element":
                                return False
                            raise
                        # Compact WDA results must contain IDs only, never labels/values.
                        if not isinstance(value, dict) or not value or set(value) - {
                                "ELEMENT", "element-6066-11e4-a52e-4f735466cecf"}:
                            raise WDAError("invalid_response", "Noncompact authentication result suppressed.")
                        return True
                    # Filter type/label before visible: native visibility can stall
                    # on unrelated elements in long Settings or custom pages.
                    # An empty Application/Window tree cannot prove that an auth
                    # heading disappeared. Require actual noneditable UI content.
                    content = ("type == 'XCUIElementTypeIcon' AND visible == 1" if home_only else
                               "(type == 'XCUIElementTypeButton' OR type == 'XCUIElementTypeStaticText' OR type == 'XCUIElementTypeNavigationBar' OR type == 'XCUIElementTypeTabBar') AND visible == 1")
                    if not exists(content):
                        return False
                    if exists("(type == 'XCUIElementTypeSecureTextField' OR type == 'XCUIElementTypeKeyboard' OR type == 'XCUIElementTypeAlert' OR type == 'XCUIElementTypeSheet') AND visible == 1"):
                        return False
                    def label_predicate(label):
                        # Never match editable field values or return text attributes.
                        literal = json.dumps(label, ensure_ascii=False)
                        return f"type != 'XCUIElementTypeTextField' AND type != 'XCUIElementTypeSecureTextField' AND type != 'XCUIElementTypeTextView' AND (label == {literal} OR name == {literal}) AND visible == 1"
                    if auth_label and exists(label_predicate(auth_label)):
                        return False
                    if ready_label and not exists(label_predicate(ready_label)):
                        return False
                    return (request("GET", "/wda/activeAppInfo") or {}).get("bundleId") == bundle_id and request("GET", "/wda/locked") is False
                finally:
                    # Restore even when the bounded check runs out of time. If this
                    # fails the caller leaves access paused; a later check can retry.
                    # All normal plugin sessions require rich responses. Restore that
                    # baseline even after a previous probe failed to restore it.
                    client.request("POST", prefix + "/appium/settings", {"settings": {"shouldUseCompactResponses": SESSION_SETTINGS["shouldUseCompactResponses"]}}, timeout=3)
            finally:
                client.close()
