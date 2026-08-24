# unified-quant

Pluggable quant data platform with a canonical, PIT-aware contract layer.

## Core Flow

```text
TDX / Tushare / AkShare / custom source
  -> source adapters
  -> canonical schema + capability routing
  -> quality gate + merge policy
  -> PIT store + dataset manifest
  -> factors / models / Qlib exporter
```

Source adapters never become the factor API. Consumers depend on versioned
canonical datasets and declared field capabilities.

## Real-Chain Research Command

```bash
uq-ingest daily --date YYYY-MM-DD --data-root /path/to/data
```

The current credential-free path uses Mootdx/TDX as the primary source and the
frozen `bars_daily.research-v1` schema. Outputs remain research prototype data;
they require real-sample validation before wider use.
