"""Deterministic coverage reconciliation against a historical dictionary snapshot."""

from collections import Counter
import json
from typing import Any


def _provenance(
    word: str, selected_provenance: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    evidence = selected_provenance.get(word)
    if not evidence:
        raise ValueError(f"Missing selected source provenance for {word!r}")
    for item in evidence:
        if not isinstance(item, dict) or not item.get("source_id"):
            raise ValueError(f"Invalid source provenance for {word!r}")
        if type(item.get("line")) is not int or item["line"] < 1:
            raise ValueError(f"Missing source line for {word!r}")
        if not item.get("source_key") or not item.get("raw_syllables"):
            raise ValueError(f"Missing source pronunciation evidence for {word!r}")
    return sorted(
        (dict(item) for item in evidence),
        key=lambda item: (
            item["source_id"], item["line"], item["source_key"],
            item["raw_syllables"], -item.get("priority", 0),
        ),
    )


def audit_coverage(
    baseline: dict[str, list[str]],
    initial: dict[str, list[str]],
    candidate: dict[str, list[str]],
    selected_provenance: dict[str, list[dict[str, Any]]],
    baseline_metadata: dict[str, Any],
) -> dict[str, Any]:
    """Report every initially lost key; refuse a release that reduces coverage.

    The historical snapshot establishes coverage, not pronunciation authority.
    Every restored or changed pronunciation needs selected source evidence.
    """
    missing = sorted(set(baseline) - set(candidate))
    if missing:
        examples = ", ".join(repr(word) for word in missing[:10])
        raise ValueError(
            f"coverage regression: candidate loses {len(missing)} baseline word(s): {examples}"
        )
    initial_missing = sorted(set(initial) - set(candidate))
    if initial_missing:
        examples = ", ".join(repr(word) for word in initial_missing[:10])
        raise ValueError(
            f"coverage regression: candidate loses {len(initial_missing)} initial P0 word(s): {examples}"
        )

    restored_words = sorted(set(baseline) - set(initial))
    initially_missing = []
    statuses: Counter[str] = Counter()
    old_count_mismatches = 0
    restoration_sources: Counter[str] = Counter()
    written_forms: Counter[str] = Counter()
    for word in restored_words:
        old_syllables, syllables = baseline[word], candidate[word]
        status = (
            "restored_unchanged" if old_syllables == syllables else "restored_corrected"
        )
        statuses[status] += 1
        old_count_mismatches += len(old_syllables) != len(word)
        provenance = _provenance(word, selected_provenance)
        primary = min(provenance, key=lambda item: (-item.get("priority", 0), item["source_id"], item["line"]))
        restoration_sources[primary["source_id"]] += 1
        forms = sorted({form for item in provenance for form in ("traditional", "simplified") if item.get(form) == word})
        form_category = "mixed_alias" if any("requested_alias" in item for item in provenance) else "+".join(forms) or "unspecified"
        written_forms[form_category] += 1
        initially_missing.append({
            "word": word,
            "old_syllables": list(old_syllables),
            "syllables": list(syllables),
            "status": status,
            "written_form": form_category,
            "provenance": provenance,
        })

    retained_changed = []
    for word in sorted(set(baseline) & set(initial)):
        if baseline[word] != candidate[word]:
            retained_changed.append({
                "word": word,
                "old_syllables": list(baseline[word]),
                "syllables": list(candidate[word]),
                "provenance": _provenance(word, selected_provenance),
            })

    return {
        "schema_version": 1,
        "baseline": dict(baseline_metadata),
        "summary": {
            "baseline_words": len(baseline),
            "initial_p0_words": len(initial),
            "candidate_words": len(candidate),
            "baseline_initial_common_words": len(set(baseline) & set(initial)),
            "missing_from_initial_p0_words": len(restored_words),
            "restored_unchanged_words": statuses["restored_unchanged"],
            "restored_corrected_words": statuses["restored_corrected"],
            "unresolved_words": 0,
            "missing_from_candidate_words": 0,
            "initial_p0_words_missing_from_candidate": 0,
            "restored_old_syllable_count_mismatches": old_count_mismatches,
            "retained_changed_words": len(retained_changed),
            "new_words": len(set(candidate) - set(baseline)),
            "restoration_primary_sources": dict(sorted(restoration_sources.items())),
            "restored_written_forms": dict(sorted(written_forms.items())),
        },
        "restored_words": restored_words,
        "missing_from_initial_p0": initially_missing,
        "retained_changed": retained_changed,
    }


def render_markdown(report: dict[str, Any]) -> str:
    counts = report["summary"]
    baseline = report["baseline"]
    lines = [
        "# Dictionary Coverage Audit", "",
        "The initial P0 replacement read a simplified-only CC-CEDICT extract; the released",
        "Dragonmapper-derived dictionary retained both written forms. It also lost the valid",
        "singleton `葛`. These omissions were a source-selection regression, not invalid words.", "",
        f"Baseline: `{baseline.get('ref', baseline.get('id'))}` / `{baseline.get('revision', '')}`.",
        f"Baseline CSV SHA-256: `{baseline.get('sha256', '')}`. It is comparison-only, not a pronunciation source.", "",
        "| Coverage metric | Words |", "| --- | ---: |",
        *[f"| {label} | {counts[key]:,} |" for label, key in (
            ("Released baseline", "baseline_words"), ("Initial P0", "initial_p0_words"),
            ("Missing from initial P0", "missing_from_initial_p0_words"),
            ("Restored, same normalized sequence", "restored_unchanged_words"),
            ("Restored, changed normalized sequence", "restored_corrected_words"),
            ("Final dictionary", "candidate_words"), ("New relative to baseline", "new_words"),
            ("Baseline keys still missing", "missing_from_candidate_words"),
            ("Initial P0 keys still missing", "initial_p0_words_missing_from_candidate"),
        )], "",
        "| Primary selected restoration source | Words |", "| --- | ---: |",
        *[f"| {source} | {count:,} |" for source, count in counts["restoration_primary_sources"].items()], "",
        "| Selected-source written form | Words |", "| --- | ---: |",
        *[f"| {form} | {count:,} |" for form, count in counts["restored_written_forms"].items()], "",
        "The current official snapshot has priority over historical 2019/2013 snapshots;",
        "all restoration sources have lower priority than existing P0 corrections. Historical",
        "entries preserve compatibility, not a claim of current independently verified usage.",
        "`restored_corrected` means the selected normalized sequence changed; this count is",
        "not a linguistic error-rate estimate. Accuracy uses separate hand-curated fixtures.", "",
        "`遵时養晦` is one explicit compatibility alias, bound to the current source line",
        "with paired `遵時養晦` / `遵时养晦` forms. No broad script conversion or joined",
        "legacy pronunciation is used. Explicit erhua `r5` becomes `er` only for the existing",
        "index convention; raw numbered syllables and the policy remain in provenance.", "",
        "[Full per-word evidence](coverage_audit.json) includes every original missing key,",
        "old/new syllables, selected source IDs/lines/raw syllables and alias details. Exact",
        "source revisions, dual gzip/decoded hashes and original 3.0/4.0 licenses are in",
        "[the source lock](../../../scripts/data/sources.lock.json) and [data notice](../NOTICE.md).",
        "Generation refuses key deletions even when unrelated new words increase total size.", "",
    ]
    return "\n".join(lines)


def json_bytes(report: dict[str, Any]) -> bytes:
    """Keep per-word records diffable without an 80 MB indentation-only report."""
    items = []
    encode = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    for key, value in sorted(report.items()):
        prefix = "  " + encode(key) + ": "
        if isinstance(value, list):
            body = ",\n".join("    " + encode(item) for item in value)
            items.append(prefix + "[\n" + body + "\n  ]")
        else:
            items.append(prefix + encode(value))
    return ("{\n" + ",\n".join(items) + "\n}\n").encode("utf-8")
