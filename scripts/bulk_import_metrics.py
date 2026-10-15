"""Read-only sampled memory observations for the local bulk-import benchmark."""

from copy import deepcopy
import math
import os
from pathlib import Path
import re
import sys
import threading
import time


PROC_ROOT = Path("/proc")
MEMORY_FIELDS = {"VmRSS": "rss_bytes", "VmHWM": "vm_hwm_bytes"}


def read_proc_memory(pid):
    """Return Linux RSS and lifetime high-water mark, never an invented zero."""
    try:
        status = (PROC_ROOT / str(pid) / "status").read_text(encoding="ascii")
        result = {"rss_bytes": None, "vm_hwm_bytes": None}
        for line in status.splitlines():
            name, separator, value = line.partition(":")
            if separator and name in MEMORY_FIELDS:
                parts = value.split()
                if len(parts) != 2 or parts[1] != "kB":
                    raise ValueError(f"unsupported {name} format")
                amount = int(parts[0])
                if amount < 0:
                    raise ValueError(f"negative {name}")
                result[MEMORY_FIELDS[name]] = amount * 1024
        if result["rss_bytes"] is None:
            raise ValueError("VmRSS is absent")
        return {"available": True, **result}
    except (OSError, UnicodeError, ValueError) as error:
        return {"available": False, "reason": f"cannot read /proc/{pid}/status: {error}",
                "rss_bytes": None, "vm_hwm_bytes": None}


def backend_identity(pid, database):
    """Reject remote/backend PID collisions unless the local title matches."""
    try:
        title = (PROC_ROOT / str(pid) / "cmdline").read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as error:
        return False, f"cannot verify PostgreSQL backend /proc/{pid}/cmdline: {error}"
    tokens = [token for token in re.split(r"[\x00\s]+", title) if token]
    has_postgres = any(Path(token.rstrip(":")).name == "postgres" for token in tokens)
    if not has_postgres or database not in tokens:
        return False, ("local PID is not a PostgreSQL backend for the exact database name; "
                       "remote databases and PID collisions are not measurable")
    return True, None


def _unescape_mount_path(value):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def cgroup_memory_path():
    """Locate this client's cgroup v2 memory counter without assuming mount root."""
    try:
        groups = (PROC_ROOT / "self/cgroup").read_text(encoding="utf-8").splitlines()
        cgroup = next((line.split(":", 2)[2] for line in groups if line.startswith("0::")), None)
        if cgroup is None:
            return None, "cgroup v2 membership is unavailable"
        member = Path(cgroup)
        if not member.is_absolute() or ".." in member.parts:
            return None, "cgroup v2 membership cannot be mapped safely to a local mount"
        mounts = (PROC_ROOT / "self/mountinfo").read_text(encoding="utf-8").splitlines()
        for line in mounts:
            left, separator, right = line.partition(" - ")
            fields = left.split()
            if not separator or len(fields) < 5 or not right.startswith("cgroup2 "):
                continue
            root = Path(_unescape_mount_path(fields[3]))
            mount = Path(_unescape_mount_path(fields[4]))
            try:
                relative = member.relative_to(root)
            except ValueError:
                continue
            return mount / relative / "memory.current", None
        return None, "no local cgroup v2 mount matches this process"
    except (OSError, UnicodeError, ValueError, IndexError) as error:
        return None, f"cannot locate local cgroup v2 counter: {error}"


def _read_cgroup_memory(path):
    try:
        amount = int(path.read_text(encoding="ascii").strip())
        if amount < 0:
            raise ValueError("negative memory.current")
        return {"available": True, "rss_bytes": amount, "vm_hwm_bytes": None}
    except (OSError, UnicodeError, ValueError) as error:
        return {"available": False, "reason": f"cannot read {path}: {error}"}


def _record(scope, reason="sampling has not started"):
    return {"available": False, "reason": reason, "scope": scope,
            "baseline_rss_bytes": None, "observed_peak_rss_bytes": None,
            "observed_peak_delta_bytes": None, "last_rss_bytes": None,
            "sample_count": 0, "read_error_count": 0, "observation_complete": True,
            "lifetime_vm_hwm_bytes": None,
            "lifetime_vm_hwm_scope": "process lifetime, not a per-case peak"}


class MemorySampler:
    """Observe memory during one case; sampling is a lower bound, not a true peak.

    Call summary() after leaving the context. The PostgreSQL PID must come from
    the benchmark connection, whose fresh UUID database name guards against
    sampling an unrelated local process when the connection is remote.
    """

    def __init__(self, backend_pid, database, interval_ms=10.0):
        if not isinstance(backend_pid, int) or isinstance(backend_pid, bool) or backend_pid <= 0:
            raise ValueError("backend_pid must be a positive integer")
        if not isinstance(database, str) or not database or re.search(r"[\x00\s]", database):
            raise ValueError("database must be a nonempty exact process-title token")
        if (not isinstance(interval_ms, (int, float)) or isinstance(interval_ms, bool)
                or not math.isfinite(interval_ms) or interval_ms <= 0):
            raise ValueError("interval_ms must be finite and positive")
        self.backend_pid = backend_pid
        self.database = database
        self.interval_ms = float(interval_ms)
        self.client_pid = os.getpid()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._started = False
        self._stopped = False
        self._start_time = None
        self._stop_time = None
        self._cgroup_path = None
        self._backend_enabled = False
        self._sampler_error = None
        self._records = {
            "client": _record("local Python client process"),
            "postgresql_backend": _record("verified co-located PostgreSQL backend process; excludes workers"),
            "container_cgroup": _record("whole local container/cgroup, including workers, client and background activity"),
        }
        self._records["container_cgroup"]["counter"] = "cgroup v2 memory.current, not RSS"
        self._records["container_cgroup"]["lifetime_vm_hwm_scope"] = "not applicable to cgroup memory.current"

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop()
        return False

    def start(self):
        if self._started:
            raise RuntimeError("MemorySampler is single-use")
        self._started = True
        self._start_time = time.monotonic()
        if not sys.platform.startswith("linux"):
            for record in self._records.values():
                record["reason"] = "memory sampling requires local Linux /proc and cgroup v2"
            return self
        self._backend_enabled, reason = backend_identity(self.backend_pid, self.database)
        if reason:
            self._records["postgresql_backend"]["reason"] = reason
        self._cgroup_path, reason = cgroup_memory_path()
        if reason:
            self._records["container_cgroup"]["reason"] = reason
        self._sample_once()
        self._thread = threading.Thread(target=self._run, name="bulk-import-memory", daemon=True)
        try:
            self._thread.start()
        except BaseException:
            self._stop.set()
            self._stopped = True
            self._stop_time = time.monotonic()
            raise
        return self

    def stop(self):
        if not self._started or self._stopped:
            return
        self._stop.set()
        if self._thread is not None and self._thread.ident is not None:
            self._thread.join(timeout=2.0)
        self._stop_time = time.monotonic()
        self._stopped = True
        if self._thread is not None and self._thread.is_alive():
            self._sampler_error = "sampling thread did not finish within the bounded 2 second join"

    def _run(self):
        try:
            while not self._stop.wait(self.interval_ms / 1000.0):
                self._sample_once()
        except Exception as error:
            with self._lock:
                self._sampler_error = f"sampling thread stopped after error: {error}"
            self._stop.set()

    def _observe(self, name, sample):
        with self._lock:
            record = self._records[name]
            if not sample["available"]:
                record["read_error_count"] += 1
                record["observation_complete"] = False
                record["reason"] = sample["reason"]
                return
            rss = sample["rss_bytes"]
            if record["baseline_rss_bytes"] is None:
                record["baseline_rss_bytes"] = rss
                record["observed_peak_rss_bytes"] = rss
                record["available"] = True
                if not record["read_error_count"]:
                    record.pop("reason", None)
            record["sample_count"] += 1
            record["last_rss_bytes"] = rss
            record["observed_peak_rss_bytes"] = max(record["observed_peak_rss_bytes"], rss)
            record["observed_peak_delta_bytes"] = record["observed_peak_rss_bytes"] - record["baseline_rss_bytes"]
            high_water = sample.get("vm_hwm_bytes")
            if high_water is not None:
                record["lifetime_vm_hwm_bytes"] = max(record["lifetime_vm_hwm_bytes"] or 0, high_water)

    def _sample_once(self):
        self._observe("client", read_proc_memory(self.client_pid))
        if self._backend_enabled:
            matched, reason = backend_identity(self.backend_pid, self.database)
            if matched:
                self._observe("postgresql_backend", read_proc_memory(self.backend_pid))
            else:
                self._backend_enabled = False
                self._observe("postgresql_backend", {"available": False, "reason": reason})
        if self._cgroup_path is not None:
            self._observe("container_cgroup", _read_cgroup_memory(self._cgroup_path))

    def summary(self):
        with self._lock:
            records = deepcopy(self._records)
            error = self._sampler_error
        cgroup = records["container_cgroup"]
        for key in ("baseline_rss_bytes", "observed_peak_rss_bytes", "last_rss_bytes"):
            cgroup[key.replace("rss", "memory")] = cgroup.pop(key)
        elapsed = None
        if self._start_time is not None:
            elapsed = max(0.0, (self._stop_time if self._stop_time is not None else time.monotonic()) - self._start_time)
        return {"started": self._started, "stopped": self._stopped,
                "thread_stopped": self._thread is None or not self._thread.is_alive(),
                "interval_ms": self.interval_ms, "elapsed_seconds": elapsed,
                "client_pid": self.client_pid, "backend_pid": self.backend_pid,
                "database": self.database, "cgroup_memory_path": str(self._cgroup_path) if self._cgroup_path else None,
                "sampling_scope": "interval-sampled observed lower bound; may miss short-lived allocations",
                "measurement_overhead": "read-only /proc and cgroup reads plus a Python sampling thread; included in case timing",
                "sampler_error": error, **records}
