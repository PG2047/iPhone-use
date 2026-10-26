"""Per-device cooperative lease for independent iPhone Use processes.

Codex starts one MCP process per chat. The first phone call of a process takes a
kernel ``flock`` on ``<lease_dir>/device.lock`` and keeps it while calls keep
coming; a watchdog releases it after an idle period, and process death releases
it through the kernel. Other processes queue through ticket files in FIFO order.

Everything is plain files plus ``flock``: no daemon, no socket, no database, so a
tool written in any language can join the same queue by following the layout
documented in ``DEVICE_LEASE.md``.

Layout of ``lease_dir``::

    device.lock          the lease itself (held open by the owner process)
    device-owner.json    pid, label, transport, timestamps, generation, released_at
    device-queue/        one ``<ns>-<pid>.json`` ticket per waiting process

Only this module touches these files inside iPhone Use.
"""
import contextlib
import datetime as dt
import fcntl
import json
import math
import os
from pathlib import Path
import re
import threading
import time

TRANSPORT = "wda"
LEASE_ROOT = Path.home() / ".local/share/iphone-use/leases"
READ_TOOLS = frozenset({"pua_ready", "pua_observe", "pua_find", "pua_wait"})
OBSERVING_TOOLS = frozenset({"pua_ready", "pua_observe", "pua_find"})
# Tools that never need the lease: diagnostics, catalogs, preview polling and the
# authentication wait, which are read-only for the shared screen.
UNLEASED_TOOLS = frozenset({"pua_doctor", "pua_apps", "pua_metrics", "pua_screen", "pua_screen_frame", "pua_device"})
LEASED_SETUP_ACTIONS = frozenset({"start", "stop", "build", "configure", "fetch"})
UNLEASED_SCREEN_ACTIONS = frozenset({"refresh", "viewport"})


def _env_float(name, default, low, high):
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if math.isfinite(value) and low <= value <= high else default


def settings():
    """Bounded tuning knobs, read on each access so tests can change them."""
    return {
        "idle_release_seconds": _env_float("IPHONE_USE_IDLE_RELEASE_SECONDS", 60.0, 1, 3600),
        "wait_seconds": _env_float("IPHONE_USE_WAIT_SECONDS", 40.0, 0, 55),
        "handover_settle_seconds": _env_float("IPHONE_USE_HANDOVER_SETTLE_SECONDS", 3.0, 0, 30),
        "ticket_ttl_seconds": _env_float("IPHONE_USE_TICKET_TTL_SECONDS", 30.0, 1, 300),
        "queue_hold_seconds": _env_float("IPHONE_USE_QUEUE_HOLD_SECONDS", 180.0, 0, 3600),
    }


def lease_directory(state_dir):
    """The lease lives per physical device, not per state directory.

    ``IPHONE_USE_LEASE_DIR`` overrides it. Otherwise the configured UDID selects
    ``~/.local/share/iphone-use/leases/<udid>``, so two state directories that
    drive the same phone share one lock and two phones never block each other.
    Before configuration the state directory itself holds the lease.
    """
    override = os.environ.get("IPHONE_USE_LEASE_DIR")
    if override:
        return Path(override).expanduser()
    udid = None
    try:
        udid = json.loads((Path(state_dir) / "config.json").read_text()).get("udid")
    except (OSError, ValueError, AttributeError):
        pass
    if isinstance(udid, str) and re.fullmatch(r"[A-Za-z0-9-]{8,64}", udid):
        return LEASE_ROOT / udid
    return Path(state_dir) / "lease"


def needs_lease(name, args):
    """Which calls occupy the phone: everything behind the operation lock plus service lifecycle."""
    if name in UNLEASED_TOOLS:
        return False
    if name == "pua_setup":
        return isinstance(args, dict) and args.get("action") in LEASED_SETUP_ACTIONS
    if name == "pua_screen_action":
        return isinstance(args, dict) and args.get("action") not in UNLEASED_SCREEN_ACTIONS
    return True


def is_mutation(name, args):
    """Calls that act on whatever page is on screen; blocked until a fresh read after a handover.

    The preview App's own gestures are the user's clicks on a live picture, so they
    are not blocked. Service lifecycle does not touch the page either.
    """
    if name in READ_TOOLS or name in ("pua_setup", "pua_screen_action"):
        return False
    return True


class LeaseError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = {"action_executed": False, "replay_action": False, **details}


def _alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _iso(ts):
    if not isinstance(ts, (int, float)):
        return None
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="seconds")


def _write_json(path, data):
    temp = path.with_name(path.name + "." + str(os.getpid()) + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, ensure_ascii=False)
        os.replace(temp, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temp)


def _read_json(path):
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class DeviceLease:
    """One instance per process and lease directory; every Runtime in the process shares it.

    ``flock`` treats two descriptors of the same file as two lockers even inside
    one process, so sharing the descriptor is what makes a second Runtime (tests,
    adapters, the gesture thread) cooperate instead of queueing behind itself.
    """

    _instances = {}
    _instances_lock = threading.Lock()

    @classmethod
    def get(cls, lease_dir):
        key = str(Path(lease_dir).expanduser().resolve(strict=False))
        with cls._instances_lock:
            lease = cls._instances.get(key)
            if lease is None:
                lease = cls._instances[key] = cls(key)
            lease.users += 1
            return lease

    def __init__(self, lease_dir):
        self.dir = Path(lease_dir)
        self.queue_dir = self.dir / "device-queue"
        self.lock_path = self.dir / "device.lock"
        self.owner_path = self.dir / "device-owner.json"
        self.pid = os.getpid()
        self.transport = TRANSPORT
        self.users = 0
        self.label = None
        self.auto_label = None
        self.needs_observation = False
        self.generation = None
        self.handover = None
        self._fd = None
        self._in_call = 0
        self._last_call = time.monotonic()
        self._acquired_at = None
        self._ticket = None
        self._ticket_wanted_until = 0.0
        self._lock = threading.RLock()
        self._closed = threading.Event()
        self._watchdog = None

    # ----- public API -------------------------------------------------------
    @property
    def holding(self):
        return self._fd is not None

    @contextlib.contextmanager
    def hold(self, name, args=None, wait_seconds=None):
        """Run one phone call under the lease; acquire it first when this process lacks it."""
        with self._lock:
            if not self.holding:
                self._acquire(settings()["wait_seconds"] if wait_seconds is None else wait_seconds)
            if self.needs_observation and is_mutation(name, args):
                raise LeaseError("fresh_observation_required",
                                 "Another chat used the iPhone since this chat last looked at it. Take a fresh "
                                 "pua_ready/pua_observe/pua_find first, then decide the action; nothing was executed.",
                                 generation=self.generation)
            self._in_call += 1
            self._last_call = time.monotonic()
            if name == "pua_launch_app" and isinstance(args, dict) and isinstance(args.get("bundle_id"), str):
                self.auto_label = args["bundle_id"]
        try:
            yield self
        finally:
            with self._lock:
                self._in_call -= 1
                self._last_call = time.monotonic()
                if self.holding:
                    self._write_owner(last_tool=name)

    def observed(self, app=None):
        """A fresh read of the current screen clears the handover requirement."""
        with self._lock:
            self.needs_observation = False
            if isinstance(app, str) and app and self.label is None:
                self.auto_label = app

    def claim(self, label=None, wait_seconds=None):
        with self._lock:
            if label:
                self.label = label[:60]
            if not self.holding:
                self._acquire(settings()["wait_seconds"] if wait_seconds is None else wait_seconds)
            else:
                self._write_owner()
            return self.status()

    def release(self, reason="explicit"):
        """Give the phone back now. A call in flight finishes first; this never cuts it short."""
        with self._lock:
            self._drop_ticket()
            if not self.holding:
                return False
            if self._in_call:
                return False
            self._write_owner(released_at=time.time(), release_reason=reason)
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
                self._acquired_at = None
            return True

    def waiting(self):
        """Live tickets of other processes."""
        return [t for t in self._tickets() if t["pid"] != self.pid]

    def status(self):
        with self._lock:
            owner = _read_json(self.owner_path)
            now = time.time()
            holder = None
            if owner and "released_at" not in owner and _alive(owner.get("pid")):
                holder = {"pid": owner.get("pid"), "label": owner.get("label") or "未命名任务",
                          "transport": owner.get("transport", TRANSPORT),
                          "since": _iso(owner.get("acquired_ts")),
                          "held_seconds": round(now - owner["acquired_ts"], 1) if isinstance(owner.get("acquired_ts"), (int, float)) else None,
                          "idle_seconds": round(now - owner["last_call_ts"], 1) if isinstance(owner.get("last_call_ts"), (int, float)) else None,
                          "last_tool": owner.get("last_tool"),
                          "is_me": owner.get("pid") == self.pid}
            queue = []
            for position, ticket in enumerate(self._tickets(), 1):
                queue.append({"position": position, "pid": ticket["pid"], "label": ticket.get("label") or "未命名任务",
                              "transport": ticket.get("transport", TRANSPORT),
                              "waiting_seconds": round(now - ticket["enqueued_ts"], 1) if isinstance(ticket.get("enqueued_ts"), (int, float)) else None,
                              "is_me": ticket["pid"] == self.pid})
            return {"holder": holder, "queue": queue,
                    "me": {"pid": self.pid, "holding": self.holding, "needs_observation": self.needs_observation,
                           "label": self.current_label(), "queued": any(q["is_me"] for q in queue)},
                    "idle_release_seconds": settings()["idle_release_seconds"], "lease_dir": str(self.dir)}

    def coordination(self):
        """Small note for tool results, only while someone else is waiting."""
        waiting = len(self.waiting())
        if not waiting:
            return None
        held = round(time.time() - self._acquired_at, 1) if self._acquired_at else None
        return {"waiting": waiting, "held_seconds": held, "idle_release_seconds": settings()["idle_release_seconds"]}

    def current_label(self):
        return self.label or self.auto_label or "未命名任务"

    def close(self):
        """Called by every Runtime.close(); the lease ends with the last user."""
        with self._lock:
            self.users = max(0, self.users - 1)
            if self.users:
                return
            self._closed.set()
            self.release("closed")
        with self._instances_lock:
            if self._instances.get(str(self.dir)) is self:
                del self._instances[str(self.dir)]

    # ----- acquisition ------------------------------------------------------
    def _acquire(self, wait_seconds):
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.queue_dir.mkdir(mode=0o700, exist_ok=True)
        self._start_watchdog()
        deadline = time.monotonic() + max(0.0, float(wait_seconds))
        if wait_seconds <= 0:
            # A non-blocking attempt never jumps ahead of a queue.
            self._prune()
            if not self._tickets() and self._try_flock():
                self._on_acquired()
                return
            self._raise_busy("device_held", 0)
        ticket = self._ensure_ticket()
        while True:
            self._prune()
            tickets = self._tickets()
            if not tickets or tickets[0]["path"] == ticket:
                if self._try_flock():
                    self._on_acquired()
                    return
            if time.monotonic() >= deadline:
                self._ticket_wanted_until = time.monotonic() + settings()["queue_hold_seconds"]
                self._raise_busy("device_queued", wait_seconds)
            self._refresh_ticket()
            time.sleep(0.25)

    def _raise_busy(self, code, waited):
        snapshot = self.status()
        holder = snapshot["holder"] or {}
        position = next((q["position"] for q in snapshot["queue"] if q["is_me"]), None)
        hold = settings()["queue_hold_seconds"]
        who = holder.get("label") or "another chat"
        idle = holder.get("idle_seconds")
        where = f"'{who}' (pid {holder.get('pid')}, idle {idle}s)" if holder else "another process"
        if code == "device_queued":
            message = (f"The iPhone is in use by {where}; this chat is number {position} in the queue after waiting "
                       f"{int(waited)}s. Call any phone tool again within {int(hold)}s to keep the place, or "
                       f"pua_device(action=release) to leave the queue. Tell the user who holds the phone; nothing was executed.")
        else:
            message = f"The iPhone is in use by {where}; nothing was sent."
        raise LeaseError(code, message, holder=holder or None, position=position, waiting=len(snapshot["queue"]),
                         keep_place_seconds=int(hold), idle_release_seconds=settings()["idle_release_seconds"])

    def _try_flock(self):
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def _on_acquired(self):
        previous = _read_json(self.owner_path)
        self._drop_ticket()
        now = time.time()
        handover = bool(previous) and previous.get("pid") != self.pid
        generation = previous.get("generation", 0) + 1 if previous and isinstance(previous.get("generation"), int) else 1
        self.generation = generation
        self.handover = {"from_pid": previous.get("pid"), "clean": "released_at" in previous} if handover else None
        self._acquired_at = now
        self._last_call = time.monotonic()
        self._write_owner(acquired_ts=now)
        if handover:
            self.needs_observation = True
            if "released_at" not in previous:
                # The previous holder died without releasing: one WDA request may still be in
                # flight on the phone. Let it finish before the first command of this chat.
                time.sleep(settings()["handover_settle_seconds"])

    def _write_owner(self, **extra):
        if not self.holding and "released_at" not in extra:
            return
        current = _read_json(self.owner_path) if self.holding else None
        if current and current.get("pid") != self.pid:
            current = None
        data = dict(current or {})
        data.update({"pid": self.pid, "label": self.current_label(), "transport": self.transport,
                     "generation": self.generation, "last_call_ts": time.time(), "format": 1})
        data.setdefault("acquired_ts", extra.pop("acquired_ts", self._acquired_at or time.time()))
        if "acquired_ts" in extra:
            data["acquired_ts"] = extra.pop("acquired_ts")
        data.pop("released_at", None)
        data.pop("release_reason", None)
        data.update(extra)
        try:
            _write_json(self.owner_path, data)
        except OSError:
            # The directory may already be gone (tests, uninstall); bookkeeping never blocks the unlock.
            pass

    # ----- queue ------------------------------------------------------------
    def _ensure_ticket(self):
        if self._ticket is not None and self._ticket.exists():
            self._refresh_ticket()
            return self._ticket
        path = self.queue_dir / f"{time.time_ns():020d}-{self.pid}.json"
        _write_json(path, {"pid": self.pid, "label": self.current_label(), "transport": self.transport,
                           "enqueued_ts": time.time(), "refreshed_ts": time.time(), "format": 1})
        self._ticket = path
        return path

    def _refresh_ticket(self):
        if self._ticket is None:
            return
        data = _read_json(self._ticket)
        if data is None:
            self._ticket = None
            return
        data["refreshed_ts"] = time.time()
        data["label"] = self.current_label()
        _write_json(self._ticket, data)

    def _drop_ticket(self):
        self._ticket_wanted_until = 0.0
        if self._ticket is not None:
            with contextlib.suppress(OSError):
                self._ticket.unlink()
            self._ticket = None

    def _tickets(self):
        """Live tickets in FIFO order; each carries its path."""
        ttl = settings()["ticket_ttl_seconds"]
        now = time.time()
        owner = _read_json(self.owner_path)
        holder_pid = owner.get("pid") if owner and "released_at" not in owner and _alive(owner.get("pid")) else None
        result = []
        try:
            names = sorted(os.listdir(self.queue_dir))
        except OSError:
            return result
        for name in names:
            if not name.endswith(".json") or name.endswith(".tmp"):
                continue
            path = self.queue_dir / name
            data = _read_json(path)
            if data is None:
                continue
            pid = data.get("pid")
            refreshed = data.get("refreshed_ts")
            live = _alive(pid) and isinstance(refreshed, (int, float)) and now - refreshed <= ttl and pid != holder_pid
            if live:
                result.append({**data, "path": path})
        return result

    def _prune(self):
        """Remove tickets of dead, expired or already-holding processes."""
        ttl = settings()["ticket_ttl_seconds"]
        now = time.time()
        owner = _read_json(self.owner_path)
        holder_pid = owner.get("pid") if owner and "released_at" not in owner and _alive(owner.get("pid")) else None
        try:
            names = os.listdir(self.queue_dir)
        except OSError:
            return
        for name in names:
            path = self.queue_dir / name
            if name.endswith(".tmp"):
                with contextlib.suppress(OSError):
                    if now - path.stat().st_mtime > ttl:
                        path.unlink()
                continue
            data = _read_json(path)
            pid = data.get("pid") if data else None
            refreshed = data.get("refreshed_ts") if data else None
            stale = (data is None or not _alive(pid) or not isinstance(refreshed, (int, float))
                     or now - refreshed > ttl or (pid == holder_pid and pid != self.pid))
            if stale:
                with contextlib.suppress(OSError):
                    path.unlink()

    # ----- watchdog ---------------------------------------------------------
    def _start_watchdog(self):
        if self._watchdog is not None and self._watchdog.is_alive():
            return
        self._closed.clear()
        self._watchdog = threading.Thread(target=self._watch, name="device-lease-watchdog", daemon=True)
        self._watchdog.start()

    def _watch(self):
        while not self._closed.wait(1.0):
            try:
                with self._lock:
                    now = time.monotonic()
                    if self.holding and not self._in_call and now - self._last_call >= settings()["idle_release_seconds"]:
                        self.release("idle")
                    if self._ticket is not None and not self.holding:
                        if now < self._ticket_wanted_until:
                            self._refresh_ticket()
                        else:
                            self._drop_ticket()
            except Exception:
                pass
