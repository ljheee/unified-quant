# <Subsystem Name>

> Current behavior reference. Last checked: `<YYYY-MM-DD / commit SHA>`. This page is based on source code, configuration, tests, and runtime evidence; mark unverified items as `UNKNOWN`.

## Responsibilities and Boundaries

Describe what this subsystem is responsible for, what it explicitly does not handle, and how it hands off to adjacent subsystems. Define boundaries by runtime responsibility rather than assuming they correspond to a directory.

## Authoritative Sources

List the code paths, configuration schemas/data sources, public interfaces, and related tests that carry the facts, and state which one is the source of truth. Use stable relative links; avoid copying volatile implementation details into multiple places.

## Primary Runtime Paths

Describe, in request or event order, the entry point, parsing, policy/validation, state reads or writes, downstream calls, and observable results. Describe multiple important entry points separately; do not show only the “happy path.”

## Multiple Implementations / Entry Points (when applicable)

When the same capability has multiple implementations, entry points, or platform/client variants, record which path is selected in actual deployment or at runtime and the evidence for that choice, each path's owner/handoff point, known behavioral differences, and how parity is checked within the scope affected by this change. Record status precedence, concurrency arbitration, recovery boundaries, and resource cleanup according to the project's actual mechanisms; do not force these concepts into the document when no corresponding mechanism exists. If path selection or differences have not yet been established, mark them `UNKNOWN`.

## State and Data Contracts

Document persistent state, lifecycle/caching, key transitions, concurrency/locks/watermarks, serialization/API contracts, and migration methods (only where applicable to this subsystem). Distinguish facts, conventions, and hypotheses.

## Constraints, Failures, and Compatibility

Record permissions, security boundaries, resource limits, error categories, retry/fallback conditions, recovery behavior, and compatibility constraints for older versions/platforms/clients. Explain which failures must fail closed and which retain the last valid state.

## Verification Evidence

List targeted tests/checks based on the project's real configuration and the behavior they cover; identify risks not yet covered and limitations observed in practice. Do not invent test commands that do not exist.

## Unknowns and Pending Confirmation

- `UNKNOWN`: <specific question>; evidence or maintainer decision required: <source or owner>
