# Factor Manifest Field Decision Table v1

Normative source: `config/schemas/manifests/factor_manifest.v1.json` is the sole machine-enforceable required-field list. Source-spec §6A is a human-readable summary and must not introduce fields absent from this schema.

| Field | Origin | Type/format | Required/null | Identity participation | Validation error | Fixture coverage |
|---|---|---|---|---|---|---|
| manifest_version | §6A + plan addition | const 1 | required | no | schema mistyped | valid/negative |
| input_dataset/input_schema_version/factor_set/factor_version/partition_date | §6A five-tuple | string/semver/date | required | generation | missing/mistyped/date | valid/date-negative |
| decision_time/run_visible_cutoff | §6A + plan addition | RFC3339 date-time | required | no | format/order | valid/order-negative |
| inputs | §6A | ordered array | min 1 | generation | missing/bad binding/checksum | valid/negative |
| factor_definitions | §6A | array of typed definitions | min 1 | generation and registry match | mismatch/fingerprint change | valid/fingerprint-negative |
| universe_snapshot | §6A | object or null | required nullable | generation when bound | mistyped/path/generation | valid/traversal-negative |
| row_count/columns/dtypes | §6A | integer/array/map | required | integrity metadata | empty/dtype mismatch | valid/negative |
| data_checksum_sha256/logical_fingerprint | §6A | sha256hex | required | generation | bad hash | valid |
| engine_version/code_fingerprint/serialization_profile_id | plan addition | string/sha256/string | required | code identity | missing/hash type | valid/negative |
| engine_package_provenance | plan addition | typed map | required | provenance only | field/type mismatch | valid/negative |
| run_id/created_at | §6A | UUID/RFC3339 | run-local only | excluded from generation | format | valid/run-stable test |
| quality | §6A | status/policy/report checksum | required | accepted quality decision | policy mismatch | valid/policy-negative |
| manifest_digest_sha256 | §6A | canonical JSON SHA-256 over final unsigned payload plus generation | required | digest | recompute mismatch | identity tests |
| generation_id | §6A | canonical stable content hash excluding run metadata | required | primary content identity | recompute mismatch | identity tests |
