# Raw CC-CEDICT Snapshots

These gzip files retain the original CC-CEDICT V1 records and copyright headers,
including both traditional/simplified headwords and explicit numbered syllables.
The generator reads them offline, checks compressed and decoded SHA-256 hashes,
and only restores keys in the released coverage baseline. No joined legacy
pronunciation is reused. Manual/base P0 readings have higher priority.

| Snapshot | Publisher / mirror | Original license |
| --- | --- | --- |
| 2026-10-12 08:13:53 UTC | CC-CEDICT contributors / MDBG | CC-BY-SA-4.0 |
| 2019-05-27 20:06:35 UTC | MDBG / yishn/chinese-tokenizer | CC-BY-SA-4.0 |
| 2013-08-28 08:05:53 UTC | MDBG / regisb/CC-CEDICT | CC-BY-SA-3.0 Unported |

Exact download URLs, mirror commits, priorities and dual hashes are in
[`sources.lock.json`](../../scripts/data/sources.lock.json). The official gzip
is retained as downloaded; historical raw `.u8` files were compressed with
`gzip -n -c`, without changing decoded bytes. Original snapshots may be obtained
using the pinned download URLs; later official downloads can change and must
not replace the retained snapshot without a reviewed lock update.

The 2013 snapshot covers historical entries removed from newer dictionaries.
They are preserved for compatibility, not declared newly reviewed or current.
An explicit mixed-script alias for `遵时養晦` is bound to the current source line
and validated against the paired forms, rather than inferred by broad script
conversion. See the [data notice](../../sql/data/NOTICE.md) and generated
[`coverage_audit.json`](../../sql/data/reports/coverage_audit.json).
