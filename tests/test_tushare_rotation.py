from uq.adapters.tushare_free import TushareFreeAdapter


def test_tokens_support_comma_rotation_and_legacy_fallback(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKENS", " first , second , first ")
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    assert TushareFreeAdapter()._tokens() == ["first", "second"]

    monkeypatch.delenv("TUSHARE_TOKENS")
    monkeypatch.setenv("TUSHARE_TOKEN", "legacy")
    assert TushareFreeAdapter()._tokens() == ["legacy"]


def test_client_rotates_configured_tokens(monkeypatch):
    calls = []

    class FakeProApi:
        def __init__(self, token):
            calls.append(token)

    import tushare as ts

    monkeypatch.setattr(ts, "set_token", lambda token: None)
    monkeypatch.setattr(ts, "pro_api", FakeProApi)
    monkeypatch.setenv("TUSHARE_TOKENS", "first,second")
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    adapter = TushareFreeAdapter()
    adapter._client()
    adapter._client()
    adapter._client()
    assert calls == ["first", "second", "first"]
