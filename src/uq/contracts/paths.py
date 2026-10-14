"""One symlink rule for governed stores.

Every immutable store refuses symbolic links, because a link inside a store can
point a verified manifest at bytes the manifest does not describe.  Checking
every *lexical* ancestor gets that wrong on ordinary machines: macOS keeps
``/tmp`` and ``/var`` behind ``/private``, and users symlink project directories
constantly, so a perfectly legitimate store root was refused (external review
B9) -- and the test suite never noticed, because ``pytest`` hands out an already
resolved ``tmp_path``.

The rule is therefore scoped to the store: the layout *above* the root is
someone else's business, at and below the root nothing may be a link, and the
resolved path must still land inside the resolved root.

The walk runs over the *spelled* components, so the two arguments must be
spelled consistently -- a ``path`` at or under the spelled ``root``.  A path
that is only reachable after resolution cannot be audited for in-store links:
once resolution collapses a link the component that pointed away is gone, so a
resolved ``path`` beside an unresolved ``root`` is refused rather than waved
through.  An earlier "either spelling" fallback walked the resolved path and
silently accepted an in-store link (external review B9 follow-up); callers that
hold a resolved path must re-spell it onto their store root before calling.
"""

from __future__ import annotations

from pathlib import Path

from ..errors import ContractError


def reject_symlinks_within(root: Path, path: Path, *, label: str = "store path") -> Path:
    """Return ``path`` after proving no component at or below ``root`` is a link.

    Raises :class:`ContractError` when ``path`` is not spelled at or under
    ``root``, when any component from ``root`` down is a symlink, or when
    resolution leaves the resolved root.

    ``path`` must be spelled relative to ``root`` (both unresolved, or both
    resolved): the walk descends the spelled components, and a resolved path
    would have already dropped the very link this guard exists to find.  Callers
    that hold a resolved path must re-spell it onto their store root first (see
    ``AdjustedPriceDatasetStore``).
    """
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ContractError(f"path is outside the governed root: {path}") from exc
    probe = root
    for part in relative.parts:
        probe = probe / part
        if probe.is_symlink():
            raise ContractError(f"symbolic links are forbidden in {label}: {probe}")
    resolved_root = root.resolve(strict=False)
    resolved_path = path.resolve(strict=False)
    if resolved_path != resolved_root and resolved_root not in resolved_path.parents:
        raise ContractError(f"path leaves the governed root after resolution: {path}")
    return resolved_path
