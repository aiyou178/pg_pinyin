#!/usr/bin/env python3
"""Targeted regression fixtures, not a population pronunciation accuracy estimate."""

import csv
import hashlib
import gzip
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DictionaryAccuracyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = json.loads((ROOT / "test/fixtures/pinyin_accuracy.json").read_text())
        with (ROOT / "sql/data/pinyin_words.csv").open(newline="") as fh:
            cls.words = dict(csv.reader(fh))
        with (ROOT / "sql/data/pinyin_mapping.csv").open(newline="") as fh:
            cls.chars = dict(csv.reader(fh))

    def test_exact_phrase_syllables(self):
        for fixture in self.fixtures:
            with self.subTest(text=fixture["text"]):
                self.assertEqual(self.words[fixture["text"]].split(), fixture["syllables"])

    def test_char_umlaut_first_reading(self):
        for char, expected in [("吕", "lv"), ("旅", "lv"), ("律", "lv"),
                               ("女", "nv"), ("绿", "lv"), ("虐", "nve"), ("略", "lve")]:
            with self.subTest(char=char):
                self.assertEqual(self.chars[char].strip("|").split("|")[0], expected)

    def test_all_phrases_have_character_aligned_boundaries(self):
        invalid = [word for word, reading in self.words.items()
                   if len(word) != len(reading.split())]
        self.assertEqual(invalid, [])

    def test_canonical_alphabet_and_no_duplicate_keys(self):
        for name in ("pinyin_mapping.csv", "pinyin_words.csv", "pinyin_token.csv"):
            with self.subTest(name=name):
                with (ROOT / "sql/data" / name).open(newline="") as fh:
                    rows = list(csv.reader(fh))
                self.assertEqual(len(rows), len({row[0] for row in rows}))
                if name != "pinyin_token.csv":
                    self.assertTrue(all(set(row[1]) <= set("abcdefghijklmnopqrstuvwxyz| ")
                                        for row in rows))

    def test_generated_output_hashes(self):
        # The manifest schema is checked by the generator's reproducibility tests.
        manifest = (ROOT / "sql/data/manifest.json").read_text()
        for name in ("pinyin_mapping.csv", "pinyin_words.csv", "pinyin_token.csv"):
            digest = hashlib.sha256((ROOT / "sql/data" / name).read_bytes()).hexdigest()
            self.assertIn(digest, manifest)

    def test_baseline_word_coverage_is_not_reduced(self):
        baseline_path = ROOT / "scripts/data/baselines/v0.0.8-pinyin_words.csv"
        with baseline_path.open(newline="") as fh:
            baseline = dict(csv.reader(fh))
        self.assertEqual(len(baseline), 157702)
        self.assertEqual(sorted(set(baseline) - set(self.words)), [])
        # This historical singleton is a real dictionary key, not a phrase error.
        self.assertIn("葛", self.words)

    def test_every_initial_p0_loss_is_audited_and_restored(self):
        report = json.loads((ROOT / "sql/data/reports/coverage_audit.json").read_text())
        summary = report["summary"]
        self.assertEqual(summary["baseline_words"], 157702)
        self.assertEqual(summary["initial_p0_words"], 132097)
        self.assertEqual(summary["missing_from_initial_p0_words"], 64091)
        self.assertEqual(summary["candidate_words"], len(self.words))
        self.assertEqual(summary["unresolved_words"], 0)
        self.assertEqual(summary["missing_from_candidate_words"], 0)
        audited = {entry["word"]: entry for entry in report["missing_from_initial_p0"]}
        self.assertEqual(len(audited), 64091)
        self.assertEqual(report["restored_words"], sorted(audited))
        self.assertEqual(
            summary["restored_unchanged_words"] + summary["restored_corrected_words"],
            len(audited),
        )
        for word, entry in audited.items():
            with self.subTest(word=word):
                self.assertEqual(entry["syllables"], self.words[word].split())
                self.assertIn(entry["status"], ("restored_unchanged", "restored_corrected"))
                self.assertTrue(entry["provenance"])
                for source in entry["provenance"]:
                    self.assertTrue(source["source_id"])
                    self.assertGreater(source["line"], 0)
                    self.assertTrue(source["source_key"])
                    self.assertTrue(source["raw_syllables"])
        manifest = json.loads((ROOT / "sql/data/manifest.json").read_text())
        self.assertEqual(manifest["counts"]["coverage"], summary)
        baseline_digest = hashlib.sha256(
            (ROOT / "scripts/data/baselines/v0.0.8-pinyin_words.csv").read_bytes()
        ).hexdigest()
        self.assertEqual(report["baseline"]["sha256"], baseline_digest)
        self.assertEqual(report["baseline"]["ref"], "v0.0.8")

    def test_restored_accuracy_fixtures_were_previously_lost(self):
        report = json.loads((ROOT / "sql/data/reports/coverage_audit.json").read_text())
        restored = set(report["restored_words"])
        fixtures = [case for case in self.fixtures if case["kind"] == "restored_coverage"]
        self.assertEqual(len(fixtures), 15)
        self.assertTrue(all(case["text"] in restored for case in fixtures))

    def test_all_restored_readings_replay_exact_raw_source_records(self):
        lock = json.loads((ROOT / "scripts/data/sources.lock.json").read_text())
        sources = {}
        for source in lock["phrase_sources"]:
            if not source.get("restore_baseline_only"):
                continue
            content = (ROOT / source["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(content).hexdigest(), source["sha256"])
            if source.get("compression") == "gzip":
                content = gzip.decompress(content)
                self.assertEqual(hashlib.sha256(content).hexdigest(), source["decoded_sha256"])
            sources[source["id"]] = content.decode().splitlines()
        report = json.loads((ROOT / "sql/data/reports/coverage_audit.json").read_text())
        for entry in report["missing_from_initial_p0"]:
            for evidence in entry["provenance"]:
                with self.subTest(word=entry["word"], source=evidence["source_id"]):
                    raw = sources[evidence["source_id"]][evidence["line"] - 1]
                    match = re.match(r"^(\S+)\s+(\S+)\s+\[([^\[\]]+)\]\s+/", raw)
                    self.assertIsNotNone(match)
                    traditional, simplified, numbered = match.groups()
                    self.assertIn(evidence["source_key"], (traditional, simplified))
                    self.assertEqual(numbered.split(), evidence["raw_syllables"])
                    # Independent replay for V1 numeric syllables, not generator output.
                    normalized = ["er" if token.lower() == "r5" and char in "儿兒"
                                  else re.sub(r"[0-5]$", "", token.lower().replace("u:", "v"))
                                  for char, token in zip(entry["word"], numbered.split())]
                    self.assertEqual(normalized, entry["syllables"])
                    if "requested_alias" in evidence:
                        self.assertEqual(evidence["requested_alias"], entry["word"])
                        self.assertTrue(all(char in (trad, simp) for char, trad, simp
                                            in zip(entry["word"], traditional, simplified)))


if __name__ == "__main__":
    unittest.main()
