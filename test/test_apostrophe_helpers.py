"""Standalone Python benchmark parsers preserve explicit pinyin boundaries."""

import importlib.util
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
TOKEN_FILE = ROOT / "sql/data/pinyin_token.csv"


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PHRASE = load_script("benchmark_pinyin_regex_phrase")
SEARCH = load_script("benchmark_pinyin_regex_search")


class ApostropheHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parsers = {
            "phrase": PHRASE.load_python_helper(str(TOKEN_FILE), "tokens"),
            "search": SEARCH.load_python_patterns(str(TOKEN_FILE)),
        }

    def test_standalone_parsers_match_handwritten_compat_fixture(self):
        fixtures = json.loads((ROOT / "test/fixtures/pinyin_query_modes.json").read_text(encoding="utf-8"))
        for case in fixtures["pattern_cases"]:
            if case["mode"] != "compat" or case["generated"] or case["input"] is None:
                continue
            for label, parser in self.parsers.items():
                with self.subTest(parser=label, fixture=case["label"]):
                    self.assertEqual(parser(case["input"]), case["patterns"])

    def test_explicit_boundaries_do_not_enumerate_mutations(self):
        for label, parser in self.parsers.items():
            with self.subTest(parser=label):
                self.assertEqual(parser("xian"), ["xian.*"])
                for value in ("xi'an", "xi’an", "xi‘an", "''xi’’‘‘an'''", " xi\t' an\n"):
                    self.assertEqual(parser(value), ["xi.*", "an.*"])
                self.assertEqual(parser("whh"), ["w.*", "h.*", "h.*"])

    def test_invalid_mixed_input_is_not_partially_tokenized(self):
        invalid = (
            "xi'an!", "xi-an", "xi`an", 'xi"an', "xi/an", "xi'an3",
            "xī'an", "lü'an", "xi'安", "xi'an;SELECT 1", "xi'an!\n",
        )
        for label, parser in self.parsers.items():
            for value in invalid:
                with self.subTest(parser=label, value=value):
                    self.assertEqual(parser(value), [])

    def test_empty_and_only_separator_inputs_have_no_patterns(self):
        for label, parser in self.parsers.items():
            for value in ("", " ", "'", "‘", "’", "'‘’ '\t\n"):
                with self.subTest(parser=label, value=value):
                    self.assertEqual(parser(value), [])

    def test_query_string_construction_uses_two_positions(self):
        helper = PHRASE.load_python_helper(str(TOKEN_FILE), "query")
        for value in ("xi'an", "xi’an", "xi‘an"):
            with self.subTest(value=value):
                self.assertEqual(helper(value), "pdb.regex_phrase(['xi.*', 'an.*'])")
        self.assertEqual(helper("xian"), "pdb.regex('xian.*')")
        self.assertEqual(helper("'‘’"), "pdb.empty()")


if __name__ == "__main__":
    unittest.main()
