import importlib.util
import json
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "coverage_audit.py"
SPEC = importlib.util.spec_from_file_location("coverage_audit", MODULE_PATH)
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


def evidence(word, raw, source="boundary-source", line=1):
    return {
        "source_id": source,
        "priority": 100,
        "line": line,
        "source_key": word,
        "raw_syllables": raw,
    }


class CoverageAuditTests(unittest.TestCase):
    def test_rejects_loss_even_when_new_words_replace_the_count(self):
        baseline = {"銀行": ["yin", "hang"]}
        for candidate in ({"女兒": ["nv", "er"]},
                          {"女兒": ["nv", "er"], "重慶": ["chong", "qing"]}):
            with self.subTest(candidate=candidate):
                with self.assertRaisesRegex(ValueError, "loses 1 baseline word"):
                    AUDIT.audit_coverage(baseline, {}, candidate, {}, {})

    def test_rejects_a_loss_of_new_initial_p0_coverage(self):
        baseline = {"銀行": ["yin", "hang"]}
        initial = {**baseline, "新詞": ["xin", "ci"]}
        with self.assertRaisesRegex(ValueError, "loses 1 initial P0 word"):
            AUDIT.audit_coverage(baseline, initial, baseline, {}, {})

    def test_restored_pronunciations_need_traceable_evidence(self):
        words = {"女兒": ["nv", "er"]}
        for provenance in ({}, {"女兒": [{"source_id": "source"}]},
                           {"女兒": [{**evidence("女兒", "nu:3 er2"), "line": 0}]},
                           {"女兒": [{**evidence("女兒", "nu:3 er2"), "raw_syllables": ""}]}):
            with self.subTest(provenance=provenance):
                with self.assertRaises(ValueError):
                    AUDIT.audit_coverage(words, {}, words, provenance, {})

    def test_corrected_boundaries_and_unchanged_restorations_are_separate(self):
        baseline = {"女兒": ["nver"], "銀行": ["yin", "hang"], "西安": ["xian"]}
        initial = {"西安": ["xi", "an"]}
        candidate = {"女兒": ["nv", "er"], "銀行": ["yin", "hang"], "西安": ["xi", "an"]}
        provenance = {
            "女兒": [evidence("女兒", "nu:3 er2")],
            "銀行": [evidence("銀行", "yin2 hang2")],
            "西安": [evidence("西安", "xi1 an1")],
        }
        report = AUDIT.audit_coverage(baseline, initial, candidate, provenance, {"ref": "v0.0.8"})
        self.assertEqual(report["restored_words"], ["女兒", "銀行"])
        self.assertEqual(report["summary"]["restored_corrected_words"], 1)
        self.assertEqual(report["summary"]["restored_unchanged_words"], 1)
        self.assertEqual(report["summary"]["retained_changed_words"], 1)
        self.assertEqual(report["summary"]["restored_old_syllable_count_mismatches"], 1)
        self.assertEqual(report["missing_from_initial_p0"][0]["old_syllables"], ["nver"])
        self.assertEqual(report["missing_from_initial_p0"][0]["syllables"], ["nv", "er"])

    def test_order_is_deterministic_and_single_character_coverage_is_retained(self):
        baseline = {"銀行": ["yin", "hang"], "葛": ["ge"], "女兒": ["nver"]}
        initial = {"葛": ["ge"]}
        candidate = {"女兒": ["nv", "er"], "葛": ["ge"], "銀行": ["yin", "hang"]}
        provenance = {
            "銀行": [evidence("銀行", "yin2 hang2", "z-source", 2),
                     evidence("銀行", "yin2 hang2", "a-source", 3)],
            "女兒": [evidence("女兒", "nu:3 er2")],
        }
        first = AUDIT.audit_coverage(baseline, initial, candidate, provenance, {"ref": "v0.0.8"})
        second = AUDIT.audit_coverage(
            dict(reversed(list(baseline.items()))), initial,
            dict(reversed(list(candidate.items()))),
            {word: list(reversed(rows)) for word, rows in provenance.items()},
            {"ref": "v0.0.8"},
        )
        self.assertEqual(json.dumps(first, ensure_ascii=False), json.dumps(second, ensure_ascii=False))
        self.assertEqual(first["summary"]["baseline_words"], 3)
        self.assertEqual(first["summary"]["unresolved_words"], 0)
        self.assertNotIn("葛", first["restored_words"])


if __name__ == "__main__":
    unittest.main()
