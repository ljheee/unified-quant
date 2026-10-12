# Paper Execution Research Integration Release Marker

## v1 — current release evidence

Released at `a76e89eeb9aeced723b12704d47880febfdab4e0`.

Final implementation commit: `a76e89eeb9aeced723b12704d47880febfdab4e0`.
Evidence commit: recorded as `HEAD_evidence_only` (the evidence-only commit on top).
Final local gate: 862 passed, 23 skipped (base extras `dev real`).
Final remote matrix: run `38253299816`, ten cells passed (six base + four Qlib).

Each cell's raw `gate-report.json`, `requirements.lock.txt`,
`requirements.lock.txt.sha256`, and `certification/environment-cell.txt` is archived
under `final-head-ci/38253299816/cells/<cell>/`; `aggregated-gates.json` copies every
per-cell field verbatim from that cell's own report and never restates a single
platform or Python version for the whole matrix.

Broker connectivity, live-market execution, network calls, credentials, streaming
runtimes, and production-runtime review keys remain outside this slice.
