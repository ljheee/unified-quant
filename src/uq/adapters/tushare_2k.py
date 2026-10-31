from __future__ import annotations

from .tushare import TushareAdapter


class Tushare2kAdapter(TushareAdapter):
    """Adapter for 2000-point Tushare accounts.

    Shares the TushareAdapter implementation. In addition to bars and reference
    data, ``research_data`` exposes bounded standard 2000-point research inputs
    such as ``daily_basic``, SW industry history, statements, money flow, and
    connect holdings. Premium ``*_vip`` endpoints remain outside this adapter;
    ``report_rc`` is only a 10-request/day trial at this account tier.
    """

    def __init__(self, token_env: str = "TUSHARE_2K_TOKEN") -> None:
        super().__init__(token_env=token_env)
