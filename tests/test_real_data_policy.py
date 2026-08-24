from datetime import date

from uq.market.lifecycle import AkShareLifecycleProvider
from uq.contracts.config import load_dataset_contract
from uq.contracts.schema import load_schema


class _FakeLifecycle(AkShareLifecycleProvider):
    def __init__(self, codes, delisted=None):
        self.codes = codes
        self.delisted = delisted or {}

    def _listed_codes(self):
        return self.codes

    def _delisted_codes(self):
        return self.delisted

    def suspension_window(self, trade_date, instruments, sources=("akshare",)):
        return {instrument: "suspended_expected_missing" for instrument in instruments}


class _NoSuspensionLifecycle(_FakeLifecycle):
    def suspension_window(self, trade_date, instruments, sources=("akshare",)):
        return {}


def test_lifecycle_provider_only_classifies_supported_unlisted_symbols():
    provider = _FakeLifecycle({"600000", "000001"})
    result = provider.classify_missing(
        date(2026, 8, 21),
        ["600000.XSHG", "000001.XSHE", "600001.XSHG"],
        ("akshare",),
    )
    assert result["600000.XSHG"] == "unknown_requires_review"
    assert result["000001.XSHE"] == "unknown_requires_review"
    assert result["600001.XSHG"] == "not_listed_expected_missing"


def test_delisted_symbol_is_expected_missing_after_termination_date():
    provider = _FakeLifecycle(set(), {"600001": date(2009, 12, 29)})
    result = provider.classify_missing(
        date(2026, 8, 21), ["600001.XSHG"], ("akshare",)
    )
    assert result["600001.XSHG"] == "delisted_expected_missing"


def test_future_delisted_date_stays_unknown():
    provider = _FakeLifecycle({"600001"}, {"600001": date(2027, 1, 1)})
    result = provider.classify_missing(
        date(2026, 8, 21), ["600001.XSHG"], ("akshare",)
    )
    assert result["600001.XSHG"] == "unknown_requires_review"


def test_lifecycle_requires_enabled_auxiliary_source():
    provider = AkShareLifecycleProvider()
    result = provider.classify_missing(date(2026, 8, 21), ["600000.XSHG"], ("adata",))
    assert result == {"600000.XSHG": "unknown_requires_review"}


def test_unknown_missing_defaults_to_rejected_and_is_switchable():
    schema = load_schema("config/schemas/bars_daily.research-v1.yaml")
    contract = load_dataset_contract("config/datasets/bars_daily.research-v1.yaml", schema)
    assert contract.row_policy["allow_unknown_missing"] is False


def test_suspension_evidence_is_in_expected_missing_set():
    assert "suspended_expected_missing" in AkShareLifecycleProvider.STATUS_EXPECTED_MISSING
