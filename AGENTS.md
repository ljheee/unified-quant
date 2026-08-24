# Repository Instructions

- Python code targets 3.11+ and uses modern type syntax.
- Keep source adapters isolated from canonical factor APIs.
- Never commit generated market data, Qlib `.bin` files, credentials, or data caches.
- Canonical schema changes that alter semantics or units require a new version.
- Run `python -m pytest` before handing off changes.
