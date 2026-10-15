import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("bulk_import_metrics", ROOT / "scripts/bulk_import_metrics.py")
METRICS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(METRICS)
DATABASE = "pg_pinyin_import_0123456789abcdef"


def sample(rss, hwm=None):
    return {"available": True, "rss_bytes": rss, "vm_hwm_bytes": hwm}


class BulkImportMemoryTests(unittest.TestCase):
    def test_proc_memory_converts_kib_to_bytes_and_separates_lifetime_high_water(self):
        with patch.object(Path, "read_text", return_value="Name:\tpostgres\nVmRSS:\t12 kB\nVmHWM:\t99 kB\n"):
            self.assertEqual(METRICS.read_proc_memory(41), sample(12288, 101376))

    def test_proc_memory_unavailable_is_not_zero(self):
        failures = ("Name: postgres\nVmHWM: 99 kB\n", "VmRSS: -1 kB\n", "VmRSS: 1 MB\n", "VmRSS: invalid kB\n")
        for text in failures:
            with self.subTest(text=text), patch.object(Path, "read_text", return_value=text):
                result = METRICS.read_proc_memory(41)
                self.assertFalse(result["available"])
                self.assertIsNone(result["rss_bytes"])
                self.assertIn("reason", result)
        for error in (FileNotFoundError("gone"), PermissionError("denied")):
            with self.subTest(error=error), patch.object(Path, "read_text", side_effect=error):
                self.assertFalse(METRICS.read_proc_memory(41)["available"])

    def test_missing_high_water_does_not_hide_valid_rss(self):
        with patch.object(Path, "read_text", return_value="VmRSS: 7 kB\n"):
            self.assertEqual(METRICS.read_proc_memory(41), sample(7168))

    def test_backend_requires_postgres_and_exact_database_token(self):
        for title in (f"postgres: postgres {DATABASE} [local] idle\0", f"/usr/bin/postgres\0{DATABASE}\0"):
            with self.subTest(title=title), patch.object(Path, "read_bytes", return_value=title.encode()):
                self.assertEqual(METRICS.backend_identity(42, DATABASE), (True, None))
        for title in (f"notpostgres: {DATABASE}", f"postgres: {DATABASE}_other", f"postgres: other_{DATABASE}", "postgres: otherdb"):
            with self.subTest(title=title), patch.object(Path, "read_bytes", return_value=title.encode()):
                matched, reason = METRICS.backend_identity(42, DATABASE)
                self.assertFalse(matched)
                self.assertIn("PID collisions", reason)
        with patch.object(Path, "read_bytes", side_effect=PermissionError("denied")):
            self.assertFalse(METRICS.backend_identity(42, DATABASE)[0])

    def test_cgroup_namespace_and_host_mount_roots_are_mapped(self):
        fixtures = (
            ("0::/\n", "31 20 0:28 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n", "/sys/fs/cgroup/memory.current"),
            ("0::/docker/abc\n", "31 20 0:28 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n", "/sys/fs/cgroup/docker/abc/memory.current"),
            ("0::/docker/abc\n", "31 20 0:28 /docker/abc /sys/fs/cgroup rw - cgroup2 cgroup rw\n", "/sys/fs/cgroup/memory.current"),
            ("0::/\n", "31 20 0:28 / /a\\040b rw - cgroup2 cgroup rw\n", "/a b/memory.current"),
        )
        for membership, mount, expected in fixtures:
            with self.subTest(expected=expected), patch.object(Path, "read_text", side_effect=[membership, mount]):
                self.assertEqual(METRICS.cgroup_memory_path(), (Path(expected), None))

    def test_missing_cgroup_or_invalid_membership_is_unavailable(self):
        fixtures = (("2:memory:/docker/abc\n",), ("0::/../docker/abc\n",), ("0::/docker/abc\n", "31 20 0:28 /other /sys/fs/cgroup rw - cgroup2 cgroup rw\n"))
        for fixture in fixtures:
            with self.subTest(fixture=fixture), patch.object(Path, "read_text", side_effect=fixture):
                path, reason = METRICS.cgroup_memory_path()
                self.assertIsNone(path)
                self.assertTrue(reason)
        with patch.object(Path, "read_text", side_effect=FileNotFoundError("no cgroup")):
            self.assertIsNone(METRICS.cgroup_memory_path()[0])

    def test_cgroup_counter_is_read_only_and_invalid_values_are_unavailable(self):
        path = MagicMock()
        path.read_text.return_value = "100\n"
        self.assertEqual(METRICS._read_cgroup_memory(path), sample(100))
        self.assertEqual(path.mock_calls[0].args, ())
        for value in ("-1", "invalid", ""):
            path.read_text.return_value = value
            self.assertFalse(METRICS._read_cgroup_memory(path)["available"])

    def test_records_keep_baseline_sample_max_and_partial_errors_without_fake_zero(self):
        sampler = METRICS.MemorySampler(42, DATABASE)
        sampler._observe("client", sample(100, 500))
        sampler._observe("client", sample(150, 600))
        sampler._observe("client", sample(80, 600))
        sampler._observe("client", {"available": False, "reason": "permission denied"})
        result = sampler.summary()["client"]
        self.assertTrue(result["available"])
        self.assertEqual(result["baseline_rss_bytes"], 100)
        self.assertEqual(result["observed_peak_rss_bytes"], 150)
        self.assertEqual(result["observed_peak_delta_bytes"], 50)
        self.assertEqual(result["last_rss_bytes"], 80)
        self.assertEqual(result["sample_count"], 3)
        self.assertEqual(result["read_error_count"], 1)
        self.assertFalse(result["observation_complete"])
        self.assertEqual(result["lifetime_vm_hwm_bytes"], 600)
        self.assertIn("not a per-case peak", result["lifetime_vm_hwm_scope"])
        self.assertIsNone(sampler.summary()["postgresql_backend"]["baseline_rss_bytes"])

    def test_summary_is_json_serializable_and_does_not_expose_mutable_records(self):
        sampler = METRICS.MemorySampler(42, DATABASE)
        result = sampler.summary()
        json.dumps(result)
        result["client"]["sample_count"] = 999
        self.assertEqual(sampler.summary()["client"]["sample_count"], 0)
        self.assertIn("whole local container/cgroup", result["container_cgroup"]["scope"])
        self.assertIn("not RSS", result["container_cgroup"]["counter"])
        self.assertIn("lower bound", result["sampling_scope"])

    def test_non_linux_no_proc_reads_and_unavailable_reasons(self):
        with patch.object(METRICS.sys, "platform", "darwin"), patch.object(METRICS, "read_proc_memory") as proc, patch.object(METRICS, "backend_identity") as identity:
            with METRICS.MemorySampler(42, DATABASE) as sampler:
                pass
        proc.assert_not_called()
        identity.assert_not_called()
        result = sampler.summary()
        self.assertTrue(result["thread_stopped"])
        self.assertTrue(result["stopped"])
        for name in ("client", "postgresql_backend", "container_cgroup"):
            self.assertFalse(result[name]["available"])
            peak_key = "observed_peak_memory_bytes" if name == "container_cgroup" else "observed_peak_rss_bytes"
            self.assertIsNone(result[name][peak_key])
            self.assertIn("Linux", result[name]["reason"])

    def test_mocked_thread_context_stops_and_joins_after_body_exception(self):
        thread = MagicMock()
        thread.ident = 10
        thread.is_alive.return_value = False
        with patch.object(METRICS.sys, "platform", "linux"), patch.object(METRICS.threading, "Thread", return_value=thread), patch.object(METRICS, "backend_identity", return_value=(False, "remote database")), patch.object(METRICS, "cgroup_memory_path", return_value=(None, "unavailable")), patch.object(METRICS, "read_proc_memory", return_value=sample(100, 200)):
            with self.assertRaisesRegex(RuntimeError, "case failure"):
                with METRICS.MemorySampler(42, DATABASE) as sampler:
                    raise RuntimeError("case failure")
        thread.start.assert_called_once()
        thread.join.assert_called_once_with(timeout=2.0)
        self.assertTrue(sampler._stop.is_set())
        self.assertTrue(sampler.summary()["thread_stopped"])
        self.assertEqual(sampler.summary()["client"]["sample_count"], 1)
        self.assertFalse(sampler.summary()["postgresql_backend"]["available"])
        self.assertIn("remote", sampler.summary()["postgresql_backend"]["reason"])
        sampler.stop()
        thread.join.assert_called_once()

    def test_pid_reuse_stops_backend_reads_without_discarding_observations(self):
        sampler = METRICS.MemorySampler(42, DATABASE)
        sampler._backend_enabled = True
        with patch.object(METRICS, "backend_identity", side_effect=[(True, None), (False, "PID collision")]) as identity, patch.object(METRICS, "read_proc_memory", return_value=sample(100)) as proc:
            sampler._sample_once()
            sampler._sample_once()
            sampler._sample_once()
        self.assertEqual(identity.call_count, 2)
        self.assertEqual(proc.call_count, 4)
        result = sampler.summary()["postgresql_backend"]
        self.assertEqual(result["sample_count"], 1)
        self.assertEqual(result["read_error_count"], 1)
        self.assertFalse(result["observation_complete"])
        self.assertIn("PID collision", result["reason"])

    def test_run_uses_event_interval_and_exits_promptly_on_stop(self):
        sampler = METRICS.MemorySampler(42, DATABASE, interval_ms=10)
        sampler._stop = MagicMock()
        sampler._stop.wait.side_effect = [False, False, True]
        with patch.object(sampler, "_sample_once") as take:
            sampler._run()
        self.assertEqual(take.call_count, 2)
        self.assertEqual(sampler._stop.wait.call_count, 3)
        sampler._stop.wait.assert_called_with(0.01)

    def test_thread_error_is_recorded_and_stops_sampling(self):
        sampler = METRICS.MemorySampler(42, DATABASE)
        sampler._stop = MagicMock()
        sampler._stop.wait.return_value = False
        with patch.object(sampler, "_sample_once", side_effect=RuntimeError("read failure")):
            sampler._run()
        sampler._stop.set.assert_called_once()
        self.assertIn("read failure", sampler.summary()["sampler_error"])

    def test_invalid_arguments_are_rejected(self):
        for pid in (0, -1, 1.5, True):
            with self.subTest(pid=pid), self.assertRaises(ValueError):
                METRICS.MemorySampler(pid, DATABASE)
        for database in ("", None, "db name", "db\0name"):
            with self.subTest(database=database), self.assertRaises(ValueError):
                METRICS.MemorySampler(42, database)
        for interval in (0, -1, float("inf"), float("nan"), "10", True):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                METRICS.MemorySampler(42, DATABASE, interval)


if __name__ == "__main__":
    unittest.main()
