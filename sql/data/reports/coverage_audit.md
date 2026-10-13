# Dictionary Coverage Audit

The initial P0 replacement read a simplified-only CC-CEDICT extract; the released
Dragonmapper-derived dictionary retained both written forms. It also lost the valid
singleton `葛`. These omissions were a source-selection regression, not invalid words.

Baseline: `v0.0.8` / `7398408499cab61c6bcbc047804d72f00fd71f6f`.
Baseline CSV SHA-256: `c296b883c47b61ab8e6cfa051ba8e4f9456704f9408d383d0462e4294e66decf`. It is comparison-only, not a pronunciation source.

| Coverage metric | Words |
| --- | ---: |
| Released baseline | 157,702 |
| Initial P0 | 132,097 |
| Missing from initial P0 | 64,091 |
| Restored, same normalized sequence | 61,868 |
| Restored, changed normalized sequence | 2,223 |
| Final dictionary | 196,188 |
| New relative to baseline | 38,486 |
| Baseline keys still missing | 0 |
| Initial P0 keys still missing | 0 |

| Primary selected restoration source | Words |
| --- | ---: |
| cc-cedict-historical-2013 | 1,617 |
| cc-cedict-historical-2019 | 3,518 |
| cc-cedict-official-2026 | 58,956 |

| Selected-source written form | Words |
| --- | ---: |
| mixed_alias | 1 |
| simplified | 823 |
| simplified+traditional | 229 |
| traditional | 63,038 |

The current official snapshot has priority over historical 2019/2013 snapshots;
all restoration sources have lower priority than existing P0 corrections. Historical
entries preserve compatibility, not a claim of current independently verified usage.
`restored_corrected` means the selected normalized sequence changed; this count is
not a linguistic error-rate estimate. Accuracy uses separate hand-curated fixtures.

`遵时養晦` is one explicit compatibility alias, bound to the current source line
with paired `遵時養晦` / `遵时养晦` forms. No broad script conversion or joined
legacy pronunciation is used. Explicit erhua `r5` becomes `er` only for the existing
index convention; raw numbered syllables and the policy remain in provenance.

[Full per-word evidence](coverage_audit.json) includes every original missing key,
old/new syllables, selected source IDs/lines/raw syllables and alias details. Exact
source revisions, dual gzip/decoded hashes and original 3.0/4.0 licenses are in
[the source lock](../../../scripts/data/sources.lock.json) and [data notice](../NOTICE.md).
Generation refuses key deletions even when unrelated new words increase total size.
