"""Regression tests for normalization, source boundaries, and locked imports."""

import hashlib
import gzip
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import generate_extension_data as data


CHARACTERS = """U+897F: xī
U+5B89: ān
U+5584: shàn
U+611F: gǎn
U+963F: ā
U+5976: nǎi
U+5415: lǚ
U+5973: nǚ
U+7EFF: lǜ
U+671D: zhāo,cháo
U+9633: yáng
U+5C45: jū
U+66F2: qǔ
U+9700: xū
"""


class SourceFixture:
    def __init__(self, root, phrases=None):
        self.root = root
        self.lock_path = root / "sources.lock.json"
        self.sources = []
        self.character = self.write_source("characters", CHARACTERS)
        for source_id, priority, text in phrases or [("phrases", 100, "西安: xī ān\n善感: shàn gǎn\n阿奶: ā nǎi\n吕女: lǚ nǚ\n")]:
            self.sources.append({**self.write_source(source_id, text), "priority": priority, "format": "phrase"})
        self.write_lock()

    def write_source(self, source_id, text):
        path = self.root / f"{source_id}.txt"
        path.write_text(text, encoding="utf-8")
        return {
            "id": source_id, "path": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "repository": "https://example.invalid/dictionary", "revision": "fixture-pinned-revision",
            "license": "MIT",
        }

    def write_lock(self):
        lock = {"schema_version": 1, "character_source": self.character, "phrase_sources": self.sources}
        if hasattr(self, "baseline"):
            lock["coverage_baseline"] = self.baseline
        self.lock_path.write_text(json.dumps(lock), encoding="utf-8")

    def build(self):
        return data.build_dataset(self.lock_path, self.root)


class NormalizationTests(unittest.TestCase):
    def test_preserves_umlaut_before_stripping_tone(self):
        for raw in ("ü", "ǖ", "ǘ", "ǚ", "ǜ", "u\u0308", "u\u0308\u030c", "u:", "v", "v3", "ü4", "u:2"):
            with self.subTest(raw=raw):
                self.assertEqual(data.normalize_tone(raw), "v")
        self.assertEqual(data.normalize_tone("lǚ"), "lv")
        self.assertEqual(data.normalize_tone("nu\u0308\u030c"), "nv")

    def test_canonical_jqx_and_neutral_tone(self):
        for raw, expected in (("jū", "ju"), ("qu3", "qu"), ("xù", "xu"), ("ma0", "ma"), ("ma5", "ma"), ("ê", "e"), ("ń", "n")):
            with self.subTest(raw=raw):
                self.assertEqual(data.normalize_tone(raw), expected)

    def test_invalid_punctuation_and_tone_numbers_are_not_stripped(self):
        for raw in ("xi'an", "xi-an", "xi an", "ma6", "m2a", "", "ä", "lu!"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    data.normalize_tone(raw)


class ImportTests(unittest.TestCase):
    def test_preserves_source_syllables_and_character_normalization(self):
        with tempfile.TemporaryDirectory() as directory:
            result = SourceFixture(Path(directory)).build()
            self.assertEqual(result.word_map["西安"], ["xi", "an"])
            self.assertEqual(result.word_map["善感"], ["shan", "gan"])
            self.assertEqual(result.word_map["阿奶"], ["a", "nai"])
            self.assertEqual(result.word_map["吕女"], ["lv", "nv"])
            self.assertEqual(result.char_map["吕"], ["lv"])
            self.assertIn(("lv", 1), result.token_rows)
            self.assertIn(("nv", 1), result.token_rows)

    def test_priorities_variants_and_duplicate_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = SourceFixture(Path(directory), [
                ("low", 100, "朝阳: cháo yáng\n朝阳: cháo yáng\n"),
                ("high", 300, "朝阳: zhāo yáng\n朝阳_2: cháo yáng\n"),
            ])
            first = fixture.build()
            fixture.sources.reverse()
            fixture.write_lock()
            second = fixture.build()
            self.assertEqual(first.word_map["朝阳"], ["zhao", "yang"])
            self.assertEqual(first.artifacts["reports/conflicts.json"], second.artifacts["reports/conflicts.json"])
            conflicts = json.loads(first.artifacts["reports/conflicts.json"])
            self.assertEqual(len(conflicts), 1)
            self.assertEqual(conflicts[0]["selected"]["syllables"], ["zhao", "yang"])
            alternatives = conflicts[0]["alternatives"]
            self.assertEqual(alternatives[0]["syllables"], ["chao", "yang"])
            self.assertEqual(len(alternatives[0]["provenance"]), 3)
            self.assertEqual(alternatives[0]["provenance"][0]["source_key"], "朝阳_2")

    def test_rejections_are_explicit_and_mismatches_are_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = SourceFixture(Path(directory), [("phrases", 100,
                "西安: xī ān\n善感: shàng gǎn\n阿奶: ā nǎi\n"
                "吕女: lǚ xī\n安: ān\n西A: xī ān\n西安: xian\n"
                "西安: xi! an\n西安: foobar an\nmalformed\n")])
            result = fixture.build()
            reasons = result.manifest["counts"]["rejections_by_reason"]
            self.assertEqual(reasons["singleton_or_empty"], 1)
            self.assertEqual(reasons["non_han_word"], 1)
            self.assertEqual(reasons["syllable_count_mismatch"], 1)
            self.assertEqual(reasons["invalid_syllable"], 1)
            self.assertEqual(reasons["unknown_syllable"], 2)
            self.assertEqual(reasons["malformed_phrase_line"], 1)
            self.assertEqual(result.word_map["吕女"], ["lv", "xi"])
            mismatch = json.loads(result.artifacts["reports/char_reading_mismatches.json"])
            self.assertEqual(mismatch[0]["word"], "吕女")
            self.assertEqual(mismatch[0]["mismatches"][0]["character"], "女")

    def test_missing_and_modified_sources_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = SourceFixture(root)
            output = root / "output"
            output.mkdir()
            existing = output / "pinyin_words.csv"
            existing.write_bytes(b"keep existing dictionary\n")
            (root / "phrases.txt").write_text("modified", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                data.generate_data(fixture.lock_path, output, repo_root=root)
            self.assertEqual(existing.read_bytes(), b"keep existing dictionary\n")
            (root / "phrases.txt").unlink()
            with self.assertRaises(FileNotFoundError):
                data.generate_data(fixture.lock_path, output, repo_root=root)
            self.assertEqual(existing.read_bytes(), b"keep existing dictionary\n")
            self.assertEqual(list(output.iterdir()), [existing])

    def test_empty_phrase_import_refuses_to_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = SourceFixture(root, [("phrases", 100, "# no usable phrases\n")])
            output = root / "output"
            output.mkdir()
            existing = output / "pinyin_words.csv"
            existing.write_bytes(b"keep\n")
            with self.assertRaisesRegex(ValueError, "refusing empty word output"):
                data.generate_data(fixture.lock_path, output, repo_root=root)
            self.assertEqual(existing.read_bytes(), b"keep\n")

    def test_two_rebuilds_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = SourceFixture(root)
            for output in (root / "one", root / "two"):
                data.generate_data(fixture.lock_path, output, repo_root=root)
            first = {path.relative_to(root / "one"): path.read_bytes() for path in (root / "one").rglob("*") if path.is_file()}
            second = {path.relative_to(root / "two"): path.read_bytes() for path in (root / "two").rglob("*") if path.is_file()}
            self.assertEqual(first, second)
            self.assertEqual(len(first), 7)
            self.assertNotIn(str(root), first[Path("manifest.json")].decode("utf-8"))

    def test_separated_csv_import_does_not_guess_joined_syllables(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = SourceFixture(root)
            fixture.sources = [{**fixture.write_source("separated", "西安,xi an\n善感,shangan\n"), "priority": 100, "format": "csv"}]
            fixture.write_lock()
            result = fixture.build()
            self.assertEqual(result.word_map, {"西安": ["xi", "an"]})
            self.assertEqual(result.manifest["counts"]["rejections_by_reason"], {"syllable_count_mismatch": 1})

    def test_cedict_retains_both_written_forms_and_numeric_syllables(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = SourceFixture(root)
            fixture.sources.append({**fixture.write_source("cedict",
                "呂女 吕女 [Lu:3 nu:3] /fixture # is a definition character/\n西安 西安 [Xi1 an1] /fixture/\n"),
                "priority": 50, "format": "cedict"})
            fixture.write_lock()
            result = fixture.build()
            self.assertEqual(result.word_map["呂女"], ["lv", "nv"])
            self.assertEqual(result.word_map["吕女"], ["lv", "nv"])
            self.assertEqual(result.word_map["西安"], ["xi", "an"])

    def test_restoration_is_baseline_scoped_and_cannot_replace_core_readings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = SourceFixture(root, [("core", 100, "朝阳: zhāo yáng\n西安: xī ān\n")])
            fixture.baseline = fixture.write_source("baseline", "朝阳,chao yang\n吕女,lu nu\n")
            fixture.sources.append({**fixture.write_source("restore",
                "朝阳 朝阳 [chao2 yang2] /fixture/\n吕女 吕女 [lu:3 nu:3] /fixture/\n善感 善感 [shan4 gan3] /not requested/\n"),
                "priority": 50, "format": "cedict", "restore_baseline_only": True})
            fixture.write_lock()
            result = fixture.build()
            self.assertEqual(result.word_map["朝阳"], ["zhao", "yang"])
            self.assertEqual(result.word_map["吕女"], ["lv", "nv"])
            self.assertNotIn("善感", result.word_map)
            audit = json.loads(result.artifacts["reports/coverage_audit.json"])
            self.assertEqual(audit["restored_words"], ["吕女"])
            entry = audit["missing_from_initial_p0"][0]
            self.assertEqual(entry["provenance"][0]["raw_syllables"], ["lu:3", "nu:3"])

    def test_coverage_failure_does_not_overwrite_and_baseline_is_hash_locked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = SourceFixture(root)
            fixture.baseline = fixture.write_source("baseline", "朝阳,chao yang\n")
            fixture.write_lock()
            output = root / "output"
            output.mkdir()
            existing = output / "pinyin_words.csv"
            existing.write_bytes(b"keep\n")
            with self.assertRaisesRegex(ValueError, "coverage"):
                data.generate_data(fixture.lock_path, output, repo_root=root)
            self.assertEqual(existing.read_bytes(), b"keep\n")
            (root / "baseline.txt").write_bytes(b"modified\n")
            with self.assertRaisesRegex(ValueError, "checksum"):
                fixture.build()

    def test_cedict_erhua_keeps_raw_evidence_and_explicit_alignment_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = SourceFixture(root)
            fixture.sources.append({**fixture.write_source("cedict", "女兒 女儿 [nu:3 r5] /fixture/\n"),
                "priority": 50, "format": "cedict"})
            # Only the global inventory is required; missing char readings are diagnostics.
            fixture.character = fixture.write_source("characters", CHARACTERS + "U+513F: ér\n")
            fixture.write_lock()
            result = fixture.build()
            self.assertEqual(result.word_map["女兒"], ["nv", "er"])
            mismatch = json.loads(result.artifacts["reports/char_reading_mismatches.json"])
            self.assertEqual(mismatch[0]["provenance"][0]["raw_syllables"], ["nu:3", "r5"])
            self.assertEqual(mismatch[0]["provenance"][0]["alignment_policy"], "cedict-erhua-r5-to-er-compatibility")

    def test_compressed_sources_require_both_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = SourceFixture(root)
            content = "西安 西安 [Xi1 an1] /fixture/\n".encode()
            path = root / "raw.gz"
            path.write_bytes(gzip.compress(content, mtime=0))
            fixture.sources.append({
                **fixture.write_source("raw", "unused"), "path": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "decoded_sha256": hashlib.sha256(content).hexdigest(),
                "compression": "gzip", "priority": 50, "format": "cedict",
            })
            fixture.write_lock()
            self.assertEqual(fixture.build().word_map["西安"], ["xi", "an"])
            fixture.sources[-1]["decoded_sha256"] = "invalid"
            fixture.write_lock()
            with self.assertRaisesRegex(ValueError, "decoded source checksum"):
                fixture.build()

    def test_mixed_script_alias_is_line_bound_and_keeps_actual_source_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = SourceFixture(root, [("core", 100, "西安: xī ān\n")])
            fixture.baseline = fixture.write_source("baseline", "呂绿,lu lu\n")
            alias = {"word": "呂绿", "source_key": "吕绿", "line": 1, "reason": "test legacy alias"}
            fixture.sources.append({**fixture.write_source("restore", "呂綠 吕绿 [lu:3 lu:4] /fixture/\n"),
                "format": "cedict", "priority": 50, "restore_baseline_only": True,
                "headword_aliases": [alias]})
            fixture.write_lock()
            result = fixture.build()
            self.assertEqual(result.word_map["呂绿"], ["lv", "lv"])
            entry = json.loads(result.artifacts["reports/coverage_audit.json"])["missing_from_initial_p0"][0]
            self.assertEqual(entry["provenance"][0]["source_key"], "吕绿")
            self.assertEqual(entry["provenance"][0]["requested_alias"], "呂绿")
            alias["word"] = "女绿"
            fixture.write_lock()
            with self.assertRaisesRegex(ValueError, "invalid CC-CEDICT headword alias"):
                fixture.build()
            alias["word"], alias["line"] = "呂绿", 2
            fixture.write_lock()
            with self.assertRaisesRegex(ValueError, "unresolved CC-CEDICT headword aliases"):
                fixture.build()

    def test_initial_p0_reference_cannot_silently_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = SourceFixture(root)
            fixture.baseline = fixture.write_source("baseline", "西安,xian\n")
            fixture.write_lock()
            lock = json.loads(fixture.lock_path.read_text())
            lock["initial_p0"] = {"source_ids": ["phrases"], "words_sha256": "invalid"}
            fixture.lock_path.write_text(json.dumps(lock))
            with self.assertRaisesRegex(ValueError, "initial P0 coverage snapshot checksum"):
                fixture.build()


if __name__ == "__main__":
    unittest.main()
