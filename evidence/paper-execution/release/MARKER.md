# Paper Execution Layer Release Marker

## v1 — superseded by final CR

This historical entry is retained for append-only audit. Its implementation
commit was invalid and must not be used as release evidence.

Released at `b0614dc99d4d0be5528b703de38bcc0273e95cce`.

Final implementation commit: `3fc835636655a4d2651237fc4d0e9165066e0322`.

Final local base gate: 560 passed, 22 skipped.
Final local Qlib gate: 582 passed.
Final remote CI: run `34353911518`, ten cells passed.

Broker connectivity, live-market execution, network calls, credentials, and
streaming runtimes remain prohibited by the released paper execution contract.

## v2 — evidence reconciliation

Release record v2 corrected the invalid implementation-commit binding and
retained the complete v1 evidence snapshot under `final-cr/invalid-6e8a37c/`.

## v3 — current final CR evidence

Released at `b0614dc99d4d0be5528b703de38bcc0273e95cce`.

Final implementation commit: `b0614dc99d4d0be5528b703de38bcc0273e95cce`.
Evidence commit: `9aababa3c475f53858ce9b0c6881b2ddc5967e48`.
Initial release CI: run `34354376483`.
Final release CI: run `34356489780`.

Final local base gate: 561 passed, 22 skipped.
Final local Qlib gate: 582 passed.
Final remote matrix: run `34353911518`, ten cells passed.

The v1 marker and v1 evidence snapshots remain immutable historical evidence.
The current release record is v3 and binds both local gates to `b0614dc`.

Broker connectivity, live-market execution, network calls, credentials, and
streaming runtimes remain prohibited by the released paper execution contract.
