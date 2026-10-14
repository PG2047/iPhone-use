"""Shared authentication gate. MCP arguments cannot grant user consent.

The trusted boundary is the installed local runtime and the Mac user. A process
with arbitrary code/file access as that user remains outside this MCP boundary.
"""
import contextlib
import fcntl
import os
import subprocess
import threading
from wda_client import WDAError


def paused_error(uncertain=False):
    return WDAError("access_paused", "Phone access is paused. Finish authentication on the iPhone, then request resume and confirm the native Mac dialog.",
                    uncertain=uncertain, details={"action_executed": "unknown" if uncertain else False,
                    "response_suppressed": True, "replay_action": False})


class AccessGate:
    def __init__(self, screen):
        self.screen = screen
        self.local = threading.local()
        self.confirmation = None
        self.confirmation_lock = threading.Lock()

    @contextlib.contextmanager
    def state(self):
        # Same lock as pause/resume. Never hold it across a WDA request.
        fd = os.open(self.screen._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield self.screen._read_state()
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def capture_epoch(self):
        with self.state() as state:
            return state["access_epoch"]

    def begin(self, name, args, received_epoch=None):
        safe = name in ("pua_screen", "pua_screen_frame", "pua_metrics") or (
            name == "pua_setup" and args.get("action") in ("status", "stop"))
        with self.state() as state:
            token = {"epoch": state["access_epoch"], "safe": safe, "mutation": False,
                     "preview": name == "pua_screen_frame",
                     "lock_probe": name == "pua_ready" and state["pause_reason"] == "device_locked"}
            self.local.token = token
            if received_epoch is not None and received_epoch != state["access_epoch"] and not safe:
                raise paused_error()
            if state["paused"] and not (safe or token["lock_probe"]):
                raise paused_error()
            return token

    def token(self):
        return getattr(self.local, "token", None)

    def check(self, token=None, state=None, method=None, path=None):
        token = token if token is not None else self.token()
        if token is None or (token["safe"] and not token.get("preview")):
            return
        if state is None:
            with self.state() as current:
                return self.check(token, current, method, path)
        lock_probe = token["lock_probe"] and state["pause_reason"] == "device_locked" and (
            method == "GET" and path in ("/status", "/wda/locked"))
        if state["access_epoch"] != token["epoch"] or (state["paused"] and not lock_probe and not token.get("preview")):
            raise paused_error(token["mutation"])

    def before_request(self, method, path, readonly):
        with self.state() as state:
            self.check(state=state, method=method, path=path)
            token = self.token()
            if token is not None and not readonly:
                token["mutation"] = True

    def after_request(self, method, path):
        self.check(method=method, path=path)

    def unlocked(self, pause_id):
        # A lock probe can release only its matching device-lock pause, never auth.
        token = self.token()
        with self.screen._state_transaction() as state:
            if token is not None and state["access_epoch"] == token["epoch"] and state["paused"] and (
                    state["pause_reason"] == "device_locked" and state["pause_id"] == pause_id and pause_id):
                state.update(paused=False, pause_reason=None, pause_id=None, access_epoch=state["access_epoch"] + 1)
                token.update(epoch=state["access_epoch"], lock_probe=False)
        self.check()

    @contextlib.contextmanager
    def delivery(self, token=None):
        # Linearize response output with cross-process pause, including image bytes.
        with self.state() as state:
            self.check(token, state)
            yield

    @staticmethod
    def _confirm_on_mac():
        script = '''display dialog "手机认证是否已完成？恢复后，iPhone Use 可以读取屏幕并操作手机。" with title "iPhone Use：恢复手机访问" buttons {"保持暂停", "恢复手机访问"} default button "保持暂停" cancel button "保持暂停" giving up after 120
return button returned of result'''
        try:
            result = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True,
                                    text=True, timeout=125, check=False)
            return result.returncode == 0 and result.stdout.strip() == "恢复手机访问"
        except (OSError, subprocess.SubprocessError):
            return False

    def request_resume(self):
        if not self.screen.paused():
            return {"confirmation_required": False, **self.screen.start()}
        with self.confirmation_lock:
            if self.confirmation is None or not self.confirmation.is_alive():
                pause_id = self.screen.reconnect_pause_id()
                def confirm():
                    # Cross-process deduplication; retries cannot stack dialogs.
                    path = self.screen.state_dir / ".resume-confirmation.lock"
                    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
                    try:
                        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError: return
                        if self._confirm_on_mac():
                            self.screen.resume_after_confirmation(pause_id)
                    finally:
                        os.close(fd)
                self.confirmation = threading.Thread(target=confirm, name="phone-user-consent", daemon=True)
                self.confirmation.start()
        return {"confirmation_required": True, "next_step": "Confirm Restore phone access in the native Mac dialog. Tool calls and elapsed time do not grant consent.", **self.screen.start()}
