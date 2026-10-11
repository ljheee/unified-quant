from __future__ import annotations

from .tushare import TushareAdapter, _to_instrument, _to_provider_code


class TushareFreeAdapter(TushareAdapter):
    """Adapter for free-tier Tushare accounts; the shared implementation lives in TushareAdapter."""


__all__ = ["TushareFreeAdapter", "_to_instrument", "_to_provider_code"]
