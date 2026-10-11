from __future__ import annotations

from .tushare import TushareAdapter


class Tushare2kAdapter(TushareAdapter):
    """Adapter for 2000-point Tushare accounts.

    Shares the TushareAdapter implementation. Endpoints beyond the free tier
    (for example adj_factor, stk_limit, suspend_d) are reachable here; income_vip
    is still not permitted on this account.
    """

    def __init__(self, token_env: str = "TUSHARE_2K_TOKEN") -> None:
        super().__init__(token_env=token_env)
