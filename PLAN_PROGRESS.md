# Plan Progress

Tracking implementation of features from `PLAN.md`.

## Status

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 1 | DFS channel warning | done | `is_dfs_channel()` in measure.sh, diagnostic in TUI |
| 2 | Band change detection | done | `band_set` tracked in parser, diagnostic fires on 5↔2.4 switch |
| 3 | Channel width instability | done | Warns on 80/160 MHz when signal issues or loss present |
| 4 | MCS index trend diagnostics | done | `mcs_vals` tracked, warns on drop ≥4 to below MCS 5 |
| 5 | UDP traffic monitoring | not started | New CSV file, medium effort |
| 6 | networkQuality bufferbloat | not started | New CSV file, background process |
| 7 | AWDL interface status | not started | Changes main CSV column count |
| 8 | wdutil CCA% | not started | Changes main CSV column count |
| 9 | Post-session recommendations | not started | Standalone, no CSV changes |

## Test counts

- Python: 197 passed (was 182)
- Bash: 58 passed (was 46)

## Next up

Feature 9 (post-session recommendations) — no CSV changes, standalone in `lib/report.sh`.
Then features 5-6 (new CSV files for UDP and networkQuality).
Then features 7-8 (main CSV column changes, batch together).
