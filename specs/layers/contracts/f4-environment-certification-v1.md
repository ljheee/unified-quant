# F4 Environment Certification Contract v1

## Scope

F4 certifies exact-byte reproducibility only for CI-covered locked environments.
A covered cell is one operating system runner and one Python version executed by
`scripts/run_gate.sh` with `uv sync --locked`. The current declared matrix is
macOS and Ubuntu runners across Python 3.11, 3.12, and 3.13. Windows and other
CPU or BLAS/LAPACK combinations are not certified.

## Evidence

Each successful job must preserve:

1. `.gate/gate-report.json`;
2. `.gate/requirements.lock.txt`;
3. `.gate/requirements.lock.txt.sha256`;
4. an artifact naming the matrix cell as `environment-cell.txt`.

The certification job must fail unless every declared cell has a successful
report.

## Claims

- Covered cells may claim identical staged artifact checksums inside that cell.
- Uncovered cells must be marked `not certified`.
- Cross-platform claims are limited to logical fingerprints with explicit
  tolerances; byte equality is never claimed between platforms.
