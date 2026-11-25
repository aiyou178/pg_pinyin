# Repository Instructions

- Do not use SSH to hosts. Git SSH to GitHub or GitLab is allowed.
- Implement the complete scoped change before starting tests or debugging.
- Do not add explanatory feature text to product UI unless the request explicitly asks for it.

## Docker Test Baselines

- The test images install the current `cargo-pgrx` and the previous tested
  `cargo-pgrx` release. When the extension's pgrx version changes, update the
  baseline version in both `docker/Dockerfile.test-core-trixie` and
  `docker/Dockerfile.test-trixie` in the same change.
- The current development baseline is `cargo-pgrx 0.20.0` for the `0.20.1`
  test build. Do not keep `0.19.3` as the default baseline for new test images.
- Keep versioned legacy CLI slots for archived extension sources when their
  `Cargo.toml` requires an older pgrx dependency; `test/prepare_upgrade.sh`
  selects `/opt/pgrx-<version>/bin/cargo-pgrx` from that archived manifest.
- `docker/Dockerfile.test-pg19beta4-trixie` is historical compatibility
  infrastructure for the pinned ParadeDB beta-4 build. Its `0.19.3` pin is
  intentionally separate from the current PG15-19 test-image baseline.
- Keep benchmark baseline artifacts and raw logs outside the repository unless
  a report explicitly calls for a committed summary.

## Roadmap Gate

The proposed roadmap items require explicit selection before implementation:

1. Business Top-1 and ranking evaluation with independent relevance labels.
2. Optional positional initials side field/index.
3. Further standalone SQL query-parser profiling or optimization.
4. Polyphone and domain-name data reconciliation.
5. Opt-in entity/POS-protected pinyin fields.

Bulk import benchmarking and implementation are complete. Preserve the current
default field and dictionary coverage while evaluating any future candidate.
