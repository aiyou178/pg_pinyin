#!/usr/bin/env python3
"""Build deterministic dictionaries from checksum-locked, syllable-rich sources."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import os
import re
import tempfile
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coverage_audit import audit_coverage, json_bytes as coverage_json_bytes, render_markdown

REPO_ROOT = Path(__file__).resolve().parents[2]
PINYIN_LINE_RE = re.compile(r"^U\+([0-9A-F]+):\s*([^#]+?)\s*(?:#.*)?$")
CEDICT_LINE_RE = re.compile(r"^(\S+)\s+(\S+)\s+\[([^\[\]]+)\]\s+/.*?/$")
VARIANT_SUFFIX_RE = re.compile(r"_([2-9]|[1-9][0-9]+)$")
TONE_MARKS = {"\u0300", "\u0301", "\u0304", "\u030c"}
INITIALS = [
    "zh", "ch", "sh", "b", "c", "d", "f", "g", "h", "j", "k", "l",
    "m", "n", "p", "q", "r", "s", "t", "v", "w", "x", "y", "z",
]
VOWEL_TOKENS = {"a", "e", "i", "o", "u"}


@dataclass(frozen=True)
class LockedSource:
    metadata: dict[str, Any]
    content: bytes


@dataclass
class BuildResult:
    char_map: dict[str, list[str]]
    word_map: dict[str, list[str]]
    token_rows: list[tuple[str, int]]
    artifacts: dict[str, bytes]
    manifest: dict[str, Any]


def normalize_tone(raw: str, normalizer: dict[str, str] | None = None) -> str:
    """Normalize one syllable, preserving umlaut before discarding tone marks.

    The legacy normalizer argument is accepted but no longer affects results.
    Syllable boundaries belong to the source parser, not this function.
    """
    del normalizer
    text = raw.strip().lower().replace("u:", "v")
    text = re.sub(r"[0-5]$", "", text)
    decomposed = unicodedata.normalize("NFD", text)
    chunks: list[str] = []
    i = 0
    while i < len(decomposed):
        base = decomposed[i]
        if not ("a" <= base <= "z"):
            raise ValueError(f"invalid pinyin syllable: {raw!r}")
        i += 1
        marks: list[str] = []
        while i < len(decomposed) and unicodedata.combining(decomposed[i]):
            marks.append(decomposed[i])
            i += 1
        if "\u0308" in marks:
            if base != "u" or marks.count("\u0308") != 1:
                raise ValueError(f"invalid pinyin umlaut: {raw!r}")
            base = "v"
            marks.remove("\u0308")
        for mark in marks:
            if mark not in TONE_MARKS and not (mark == "\u0302" and base == "e"):
                raise ValueError(f"invalid pinyin diacritic: {raw!r}")
        chunks.append(base)
    if not chunks:
        raise ValueError("empty pinyin syllable")
    return "".join(chunks)


def is_han(character: str) -> bool:
    code = ord(character)
    return (
        code == 0x3007
        or 0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0x20000 <= code <= 0x2FA1F
        or 0x30000 <= code <= 0x3347F
    )


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def csv_bytes(rows: Any) -> bytes:
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerows(rows)
    return output.getvalue().encode("utf-8")


def load_locked_sources(lock_path: Path, repo_root: Path = REPO_ROOT) -> tuple[dict[str, Any], list[LockedSource]]:
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if lock.get("schema_version") != 1:
        raise ValueError("unsupported sources lock schema_version")
    character = lock.get("character_source")
    phrases = lock.get("phrase_sources")
    if not isinstance(character, dict) or not isinstance(phrases, list) or not phrases:
        raise ValueError("sources lock requires a character_source and nonempty phrase_sources")
    sources: list[LockedSource] = []
    seen: set[str] = set()
    for metadata in [character, *phrases]:
        for field in ("id", "path", "sha256", "repository", "revision", "license"):
            if not isinstance(metadata.get(field), str) or not metadata[field]:
                raise ValueError(f"source is missing required string field: {field}")
        source_id = metadata["id"]
        if source_id in seen:
            raise ValueError(f"duplicate source id: {source_id}")
        seen.add(source_id)
        relative = Path(metadata["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"source path must be repository relative: {metadata['path']}")
        path = repo_root / relative
        content = path.read_bytes()
        actual = sha256(content)
        if actual != metadata["sha256"]:
            raise ValueError(f"source checksum mismatch: {source_id}: expected {metadata['sha256']}, got {actual}")
        if metadata.get("compression") == "gzip":
            content = gzip.decompress(content)
            if sha256(content) != metadata.get("decoded_sha256"):
                raise ValueError(f"decoded source checksum mismatch: {source_id}")
        elif metadata.get("compression") is not None:
            raise ValueError(f"unsupported source compression: {source_id}")
        content.decode("utf-8")
        if metadata is not character:
            if metadata.get("format") not in ("phrase", "csv", "cedict"):
                raise ValueError(f"unsupported phrase source format: {source_id}")
            if type(metadata.get("priority")) is not int:
                raise ValueError(f"phrase source requires integer priority: {source_id}")
            if "restore_baseline_only" in metadata and type(metadata["restore_baseline_only"]) is not bool:
                raise ValueError(f"restore_baseline_only must be boolean: {source_id}")
        sources.append(LockedSource(dict(metadata), content))
    return lock, sources


def reject(rejected: list[dict[str, Any]], source: LockedSource, line: int, raw: str, reason: str, **details: Any) -> None:
    rejected.append({"source_id": source.metadata["id"], "line": line, "raw": raw, "reason": reason, **details})


def parse_character_source(source: LockedSource, rejected: list[dict[str, Any]]) -> dict[str, list[str]]:
    char_map: dict[str, list[str]] = {}
    for line_number, raw in enumerate(source.content.decode("utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = PINYIN_LINE_RE.fullmatch(line)
        if not match:
            reject(rejected, source, line_number, raw, "malformed_character_line")
            continue
        codepoint, readings = match.groups()
        try:
            character = chr(int(codepoint, 16))
            normalized = list(dict.fromkeys(normalize_tone(value) for value in readings.split(",")))
        except ValueError as exc:
            reject(rejected, source, line_number, raw, "invalid_character_reading", detail=str(exc))
            continue
        if character in char_map:
            reject(rejected, source, line_number, raw, "duplicate_character", character=character)
            continue
        char_map[character] = normalized
    if not char_map:
        raise ValueError("no pinyin character mappings loaded")
    return char_map


def phrase_lines(source: LockedSource, rejected: list[dict[str, Any]]) -> Any:
    text = source.content.decode("utf-8")
    if source.metadata["format"] == "csv":
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        try:
            for row in reader:
                raw = ",".join(row)
                if not row or (len(row) == 1 and not row[0].strip()):
                    continue
                if len(row) != 2:
                    reject(rejected, source, reader.line_num, raw, "malformed_phrase_line")
                    continue
                yield reader.line_num, raw, row[0].strip(), row[1].strip(), {}
        except csv.Error as exc:
            reject(rejected, source, reader.line_num, "", "malformed_csv", detail=str(exc))
        return
    aliases = source.metadata.get("headword_aliases", [])
    pending_aliases = {item["word"] for item in aliases}
    if len(pending_aliases) != len(aliases) or (aliases and source.metadata["format"] != "cedict"):
        raise ValueError("headword aliases must be unique and attached to raw CC-CEDICT")
    for line_number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip() if source.metadata["format"] == "cedict" else raw.split("#", 1)[0].strip()
        if line.startswith("#"):
            continue
        if not line:
            continue
        if source.metadata["format"] == "cedict":
            match = CEDICT_LINE_RE.fullmatch(line)
            if not match:
                reject(rejected, source, line_number, raw, "malformed_cedict_line")
                continue
            traditional, simplified, syllables = match.groups()
            # V1 supplies explicit syllables and both written forms; never guess
            # the traditional reading by converting characters in a joined CSV.
            for word in dict.fromkeys((traditional, simplified)):
                yield line_number, raw, word, syllables, {
                    "traditional": traditional, "simplified": simplified,
                }
            for alias in aliases:
                if alias["line"] != line_number:
                    continue
                word = alias["word"]
                if (alias["source_key"] not in (traditional, simplified)
                        or len(word) != len(traditional) or len(word) != len(simplified)
                        or not all(char in (trad, simp) for char, trad, simp in zip(word, traditional, simplified))):
                    raise ValueError(f"invalid CC-CEDICT headword alias: {word}")
                pending_aliases.remove(word)
                yield line_number, raw, word, syllables, {
                    "traditional": traditional, "simplified": simplified,
                    "alias_of": alias["source_key"], "alias_reason": alias["reason"],
                }
            continue
        if ":" not in line:
            reject(rejected, source, line_number, raw, "malformed_phrase_line")
            continue
        word, syllables = line.split(":", 1)
        yield line_number, raw, word.strip(), syllables.strip(), {}
    if pending_aliases:
        raise ValueError(f"unresolved CC-CEDICT headword aliases: {sorted(pending_aliases)}")


def parse_phrase_sources(sources: list[LockedSource], char_map: dict[str, list[str]], rejected: list[dict[str, Any]], baseline: dict[str, list[str]] | None = None) -> tuple[dict[str, list[str]], list[dict[str, Any]], list[dict[str, Any]], dict[str, int], dict[str, list[dict[str, Any]]]]:
    known_syllables = {token for readings in char_map.values() for token in readings}
    candidates: dict[str, dict[tuple[str, ...], list[dict[str, Any]]]] = {}
    accepted_rows = 0
    for source in sorted(sources, key=lambda item: (-item.metadata["priority"], item.metadata["id"])):
        for line_number, raw, source_key, raw_pinyin, details in phrase_lines(source, rejected):
            word = VARIANT_SUFFIX_RE.sub("", source_key)
            if source.metadata.get("restore_baseline_only"):
                if baseline is None:
                    raise ValueError("restoration source requires a locked coverage baseline")
                if word not in baseline:
                    continue
            if not word or (len(word) < 2 and not (baseline and word in baseline)):
                reject(rejected, source, line_number, raw, "singleton_or_empty", source_key=source_key)
                continue
            if not all(is_han(character) for character in word):
                reject(rejected, source, line_number, raw, "non_han_word", source_key=source_key)
                continue
            raw_syllables = raw_pinyin.split()
            if len(raw_syllables) != len(word):
                reject(rejected, source, line_number, raw, "syllable_count_mismatch", word=word, character_count=len(word), syllable_count=len(raw_syllables))
                continue
            try:
                syllables = tuple(
                    "er" if source.metadata["format"] == "cedict" and syllable.lower() == "r5" and character in "儿兒"
                    else normalize_tone(syllable)
                    for character, syllable in zip(word, raw_syllables)
                )
            except ValueError as exc:
                reject(rejected, source, line_number, raw, "invalid_syllable", word=word, detail=str(exc))
                continue
            unknown = sorted(set(syllables) - known_syllables)
            if unknown:
                reject(rejected, source, line_number, raw, "unknown_syllable", word=word, syllables=list(syllables), unknown=unknown)
                continue
            provenance = {
                "source_id": source.metadata["id"], "priority": source.metadata["priority"],
                "line": line_number, "source_key": details.get("alias_of", source_key), "raw_syllables": raw_syllables,
                **details,
            }
            if "alias_of" in details:
                provenance["requested_alias"] = source_key
            if any(value.lower() == "r5" for value in raw_syllables):
                provenance["alignment_policy"] = "cedict-erhua-r5-to-er-compatibility"
            candidates.setdefault(word, {}).setdefault(syllables, []).append(provenance)
            accepted_rows += 1
    if not candidates:
        raise ValueError("no valid boundary-preserving phrase mappings loaded; refusing empty word output")
    word_map: dict[str, list[str]] = {}
    conflicts: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    selected_provenance: dict[str, list[dict[str, Any]]] = {}
    for word, variants in sorted(candidates.items()):
        readings = []
        for syllables, provenance in variants.items():
            provenance.sort(key=lambda item: (-item["priority"], item["source_id"], item["line"]))
            readings.append({"syllables": list(syllables), "provenance": provenance})
        readings.sort(key=lambda item: (-item["provenance"][0]["priority"], item["provenance"][0]["source_id"], item["provenance"][0]["line"], item["syllables"]))
        selected = readings[0]
        word_map[word] = selected["syllables"]
        selected_provenance[word] = selected["provenance"]
        if len(readings) > 1:
            conflicts.append({"word": word, "selected": selected, "alternatives": readings[1:]})
        for reading in readings:
            differences = [
                {"position": i, "character": character, "syllable": syllable, "known_readings": char_map.get(character, [])}
                for i, (character, syllable) in enumerate(zip(word, reading["syllables"]))
                if syllable not in char_map.get(character, [])
            ]
            if differences:
                mismatches.append({"word": word, "syllables": reading["syllables"], "selected": reading is selected, "provenance": reading["provenance"], "mismatches": differences})
    return word_map, conflicts, mismatches, {"accepted_phrase_rows": accepted_rows, "distinct_phrase_readings": sum(len(values) for values in candidates.values())}, selected_provenance


def load_coverage_baseline(metadata: dict[str, Any], repo_root: Path) -> dict[str, list[str]]:
    relative = Path(metadata["path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("coverage baseline path must be repository relative")
    content = (repo_root / relative).read_bytes()
    if sha256(content) != metadata["sha256"]:
        raise ValueError("coverage baseline checksum mismatch")
    baseline: dict[str, list[str]] = {}
    for row in csv.reader(io.StringIO(content.decode("utf-8")), strict=True):
        if len(row) != 2 or row[0] in baseline or not row[0]:
            raise ValueError("invalid or duplicate coverage baseline word")
        baseline[row[0]] = row[1].split()
    if not baseline:
        raise ValueError("coverage baseline must not be empty")
    return baseline


def build_token_rows(char_map: dict[str, list[str]], word_map: dict[str, list[str]] | None = None) -> list[tuple[str, int]]:
    syllables = {token for values in char_map.values() for token in values}
    if word_map:
        syllables.update(token for values in word_map.values() for token in values)
    initials = set(INITIALS)
    full = sorted(syllables - initials - VOWEL_TOKENS, key=lambda token: (-len(token), token))
    vowels = sorted(syllables & VOWEL_TOKENS)
    return [(token, 1) for token in full] + [(token, 3) for token in vowels] + [(token, 2) for token in INITIALS]


def build_dataset(lock_path: Path, repo_root: Path = REPO_ROOT) -> BuildResult:
    lock, sources = load_locked_sources(lock_path, repo_root)
    baseline_metadata = lock.get("coverage_baseline")
    baseline = load_coverage_baseline(baseline_metadata, repo_root) if baseline_metadata else None
    rejected: list[dict[str, Any]] = []
    char_map = parse_character_source(sources[0], rejected)
    word_map, conflicts, mismatches, counts, selected_provenance = parse_phrase_sources(sources[1:], char_map, rejected, baseline)
    token_rows = build_token_rows(char_map, word_map)
    rejected.sort(key=lambda item: (item["source_id"], item["line"], item["reason"]))
    artifacts = {
        "pinyin_mapping.csv": csv_bytes((character, f"|{'|'.join(readings)}|") for character, readings in sorted(char_map.items(), key=lambda item: ord(item[0]))),
        "pinyin_token.csv": csv_bytes(token_rows),
        "pinyin_words.csv": csv_bytes((word, " ".join(syllables)) for word, syllables in sorted(word_map.items())),
        "reports/conflicts.json": json_bytes(conflicts),
        "reports/rejected.json": json_bytes(rejected),
        "reports/char_reading_mismatches.json": json_bytes(mismatches),
    }
    coverage = None
    if baseline is not None:
        initial_metadata = lock.get("initial_p0")
        if initial_metadata:
            initial_sources = [source for source in sources[1:] if source.metadata["id"] in initial_metadata["source_ids"]]
        else:
            initial_sources = [source for source in sources[1:] if not source.metadata.get("restore_baseline_only")]
        initial, _, _, _, _ = parse_phrase_sources(initial_sources, char_map, [], None)
        if initial_metadata and sha256(csv_bytes((word, " ".join(syllables)) for word, syllables in sorted(initial.items()))) != initial_metadata["words_sha256"]:
            raise ValueError("initial P0 coverage snapshot checksum mismatch")
        coverage = audit_coverage(baseline, initial, word_map, selected_provenance, baseline_metadata)
        if initial_metadata:
            coverage["initial_p0"] = initial_metadata
        artifacts["reports/coverage_audit.json"] = coverage_json_bytes(coverage)
        artifacts["reports/coverage_audit.md"] = render_markdown(coverage).encode("utf-8")
    manifest = {
        "schema_version": 1,
        "generator_sha256": sha256(Path(__file__).read_bytes()),
        "normalization": "nfd-toneless-v1-preserve-umlaut",
        "phrase_selection": "priority-desc-source-id-asc-line-asc",
        "sources_lock_sha256": sha256(lock_path.read_bytes()),
        "sources": [source.metadata for source in sorted(sources, key=lambda item: item.metadata["id"])],
        "counts": {
            "characters": len(char_map), "token_rows": len(token_rows), "words": len(word_map),
            "conflicting_words": len(conflicts), "rejected_rows": len(rejected),
            "character_reading_mismatch_sequences": len(mismatches),
            "rejections_by_reason": dict(sorted(Counter(item["reason"] for item in rejected).items())), **counts,
        },
        "outputs": {name: {"sha256": sha256(content), "bytes": len(content)} for name, content in sorted(artifacts.items())},
    }
    if coverage is not None:
        manifest["coverage_baseline"] = baseline_metadata
        manifest["counts"]["coverage"] = coverage["summary"]
    manifest["implementation_sha256"] = {
        name: sha256(Path(__file__).with_name(name).read_bytes())
        for name in ("generate_extension_data.py", "coverage_audit.py")
    }
    artifacts["manifest.json"] = json_bytes(manifest)
    return BuildResult(char_map, word_map, token_rows, artifacts, manifest)


def generate_data(lock_path: Path, output_dir: Path, report_dir: Path | None = None, repo_root: Path = REPO_ROOT, output_paths: dict[str, Path] | None = None) -> dict[str, Any]:
    result = build_dataset(lock_path, repo_root)
    report_dir = report_dir or output_dir / "reports"
    destinations = {
        name: (report_dir / name.removeprefix("reports/") if name.startswith("reports/") else output_dir / name)
        for name in result.artifacts
    }
    destinations.update(output_paths or {})
    if len({path.resolve() for path in destinations.values()}) != len(destinations):
        raise ValueError("output paths must be distinct")
    pending: list[tuple[Path, Path]] = []
    try:
        # Stage every validated artifact before replacing any existing dictionary.
        for name, content in sorted(result.artifacts.items()):
            destination = destinations[name]
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=f".{destination.name}.", delete=False) as temporary:
                temporary.write(content)
                temporary_path = Path(temporary.name)
            temporary_path.chmod(0o644)
            pending.append((temporary_path, destination))
        for temporary, destination in pending:
            os.replace(temporary, destination)
    finally:
        for temporary, _ in pending:
            temporary.unlink(missing_ok=True)
    return result.manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources-lock", type=Path, default=REPO_ROOT / "scripts/data/sources.lock.json")
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "sql/data")
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--mapping-out", type=Path)
    parser.add_argument("--token-out", type=Path)
    parser.add_argument("--words-out", type=Path)
    parser.add_argument("--source-dir", type=Path, help="Compatibility check: must contain the lock-listed character source")
    parser.add_argument("--words-source", type=Path, help="Compatibility check: must be a lock-listed separated-syllable CSV source")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.source_dir or args.words_source:
            lock, _ = load_locked_sources(args.sources_lock)
            if args.source_dir and (args.source_dir / "pinyin.txt").resolve() != (REPO_ROOT / lock["character_source"]["path"]).resolve():
                raise ValueError("--source-dir cannot bypass the sources lock; edit a custom --sources-lock instead")
            if args.words_source and not any(source["format"] == "csv" and (REPO_ROOT / source["path"]).resolve() == args.words_source.resolve() for source in lock["phrase_sources"]):
                raise ValueError("--words-source requires a lock-listed CSV with explicit syllable boundaries")
        output_paths = {name: value for name, value in (("pinyin_mapping.csv", args.mapping_out), ("pinyin_token.csv", args.token_out), ("pinyin_words.csv", args.words_out)) if value is not None}
        manifest = generate_data(args.sources_lock, args.output_dir, args.report_dir, output_paths=output_paths)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit(f"data generation failed: {exc}") from exc
    print(json.dumps(manifest["counts"], ensure_ascii=False, sort_keys=True))
    print(f"Wrote dictionaries, manifest and reconciliation reports to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
