# Paper Execution Research Integration Release Marker

## v1 — current release evidence

Released at `f8fe690e2dc71e3069ce9692ac3418a0bb6ba435`.

Final implementation commit: `f8fe690e2dc71e3069ce9692ac3418a0bb6ba435`.
Evidence commit: recorded as `HEAD_evidence_only` (this evidence-only commit).
Final local gate: 857 passed, 23 skipped (base extras `dev real`).
Final remote matrix: run `38252277339`, ten cells passed (six base + four Qlib).

Each cell's raw `gate-report.json`, `requirements.lock.txt`,
`requirements.lock.txt.sha256`, and `certification/environment-cell.txt` is archived
under `final-head-ci/38252277339/cells/<cell>/`; `aggregated-gates.json` copies every
per-cell field verbatim from that cell's own report and never restates a single
platform or Python version for the whole matrix.

Broker connectivity, live-market execution, network calls, credentials, streaming
runtimes, and production-runtime review keys remain outside this slice.
