# HTTPS bridge contract v1

Transport: verified HTTPS, no redirects. The HA bridge initiates all requests.
Maximum request body 32 KiB; maximum 20 mappings or samples per batch. Unknown
fields and versions are rejected. Responses and pairing codes use `Cache-Control:
no-store`. The backend feature flag defaults to disabled.

## Pairing and authorization

| Method and path | Authorization | Result |
| --- | --- | --- |
| POST `/api/v1/stations/{id}/home-assistant/pairing` | EMS organization admin, session + CSRF | One-time opaque code, expiry |
| GET `/api/v1/stations/{id}/home-assistant` | Station viewer | Minimized context, health, mapping version |
| DELETE `/api/v1/stations/{id}/home-assistant` | Station admin, session + CSRF | Revoke pending code/token, erase context |
| POST `/api/v1/home-assistant/pairing/redeem` | Pairing code | Station-bound bridge token |
| PUT `/api/v1/home-assistant/bridge/mappings` | Bridge bearer | Confirm consent, set allowlist/version |
| POST `/api/v1/home-assistant/bridge/samples` | Bridge bearer | Ingest or ignore duplicate/old samples |
| DELETE `/api/v1/home-assistant/bridge` | Bridge bearer | Revoke self, erase context |

Code redemption body: `schema_version: 1`, `code`, random `instance_id` (UUID),
`instance_name` (1–80 characters). Codes have 96 random bits, expire after 10
minutes, are hashed at rest, and are consumed under a PostgreSQL row lock.
Generating a new code revokes the previous connection. Codes are bound to the
issuer's station/organization; client-supplied tenant/station overrides are rejected.

The response contains `schema_version`, `token`, `token_expires_at`, `station_id`,
`station_name`, `instance_id`, `mapping_version`. The pending token expires after
10 minutes; confirming the allowlist extends it to 90 days. The backend stores
only the token hash. Every authenticated request checks expiry, station ownership,
organization state and issuing administrator access. Re-pairing rotates credentials.

## Mapping and consent

`PUT mappings` body: `schema_version:1`, `expected_version`, `consent:true`,
`occupancy_consent:false`, `insights_consent:false`, `mappings:[...]`.

Each mapping contains `entity_id`, `kind`, `unit`, `device_class`, `state_class`,
`max_age_seconds` (30–3600, default 180), `source_validated`, and `quality`
(`estimated`, `declared`, `simulated`). Kind/domain/unit/class validation is performed
both locally and remotely. HA's local registry identity is never exported.
Changing the mapping clears observations and increments its version. Repeating
an identical update with the previous expected version is idempotent. Other
conflicts return 409 and require reconfiguration/reauthentication.

## Samples

```json
{
  "schema_version": 1,
  "mapping_version": 2,
  "samples": [{
    "entity_id": "sensor.house_power",
    "kind": "power",
    "unit": "W",
    "source": "home_assistant",
    "observed_at": "2026-09-29T12:00:00+00:00",
    "sample_id": "2026-09-29T12:00:00+00:00",
    "quality": "estimated",
    "available": true,
    "value": "0"
  }]
}
```

Timestamps come from HA `last_reported`, never transmission time. Numeric values
are decimal strings; booleans apply only to aggregate occupancy. Missing or
invalid values send `available:false`, `value:null`, `quality:unknown`. Source
quality must match the mapping. Batches are atomic; unmapped entities, units,
invalid values or timestamps more than 30 seconds in the future are rejected.
Samples older than 24 hours, duplicates and out-of-order observations are ignored.
The response includes `schema_version`, `mapping_version`, `accepted`, `ignored`.
Ingest/reconfigure/disconnect serialize on the same database row.

The backend normalizes W→kW, Wh→kWh and °F→°C with Decimal. Context is stored
separately from inverter telemetry. Occupancy false/true maps to away/home;
unavailable maps to unknown. No physical control or policy bypass is provided.

Errors: 400 invalid/replayed pairing, 401 revoked/expired credential, 409 mapping
conflict, 413 body limit, 422 invalid contract, 429 rate limit, 503 feature disabled.
No error reflects credentials or raw sample data. Clients stop after 401/409;
other transport failures use bounded exponential backoff.
