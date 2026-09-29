# Mobile read API v1

The platform owns this contract. Start with `GET /api/v1/mobile/overview`;
additional calls are needed only when opening charts, history or HA details.
All endpoints are read-only. They use retained data and never contact Deye,
an MQTT broker, Home Assistant, or device hardware during the request.

## Authentication and station scope

Use the existing human session cookie (`ems_session` by default) over HTTPS.
The existing login flow and its CSRF requirements still apply. Native token
issuance/refresh belongs to [EMS-mobile-app #8](https://github.com/bbogdan59/EMS-mobile-app/issues/8);
this contract does **not** accept device or HACS bridge tokens as user credentials.
Keep cookies in the client's secure cookie store, not in URLs or logs.

Every endpoint accepts `station_id=<uuid>` and verifies active membership and
the existing viewer-or-higher station permission. Omit the ID only when there
is exactly one accessible active station. No selection returns `404 no_station`;
multiple choices return `409 station_required`. Suspended organizations retain
read access; archived organizations are blocked for ordinary members.

## Endpoints

| GET path under `/api/v1/mobile` | Data and parameters |
| --- | --- |
| `/overview` | Live power/SOC/EV state, today/month energy with comparisons, next-24-hour PV forecast, estimated costs, unread count, up to 20 device freshness summaries, Deye and both HA integration summaries. |
| `/charts/energy` | `range=24h\|7d\|30d\|1y`, optional `resolution=15m\|1h\|1d`, optional timezone-aware `end`. Defaults: 15m, 1h, 1h, 1d respectively. Maximum 768 buckets; excessive combinations return 422. |
| `/notifications` | Current user's station inbox. `limit=1..50` (default 20), optional `unread=true` and `cursor`. Includes unread count. |
| `/charging-sessions` | Station EV charging history, stored energy totals and provenance. `limit=1..50`, optional `cursor`. These are charging sessions, not authentication sessions. |
| `/home-assistant/context` | `provider=home_assistant_bridge` (default) or `home_assistant_mqtt`; only consented, allowlisted mappings and observations. |

Notification/session cursors are signed and expire after 24 hours. They are bound
to the user, station, collection and filters. Retain those parameters on subsequent
pages. Ordering uses timestamp + UUID, so equal timestamps do not skip records.
Newly created records are excluded from an already-started traversal. Existing
records may change or disappear; unread lists are not a transactional snapshot.
Start again without a cursor to refresh. A null `next_cursor` means the final page.

## Values, calendars and provenance

- Energy, power, percentages, money and coverage serialize as **decimal strings**.
  Booleans remain booleans; HA states remain strings. Never replace null with zero.
  Parse numbers only at the client presentation boundary. Units are explicit:
  kW, kWh, %, RON; grid/battery live power uses the web convention (positive
  import/charge, negative export/discharge).
- All instants carry a UTC offset. `station.timezone` is the IANA calendar for
  today/month boundaries and comparisons. Stored daily buckets can last 23 or
  25 hours at DST transitions; SOC is a mean, never a sum.
- Chart ranges are rolling durations (24h, 7/30/365 days). They select whole
  persisted buckets whose **start** lies in `[start, end)`. Each bucket exposes
  its actual start/end; no interpolation or fabricated zero buckets. The latest
  bucket may end after the requested end. Missing history yields an empty array.
- Period KPI coverage is relative to the whole calendar period, as on the web
  dashboard; comparisons require sufficient coverage of the elapsed portion of
  both periods. A longer elapsed period than the preceding day/month suppresses
  the comparison. Comparisons use the dashboard's existing finer-grained buckets.
- `measured_at` and `received_at` come only from real observation records. For
  aggregates, forecasts, costs and stored charging summaries these are null;
  `updated_at`/`issued_at` describe derivation. Legacy aggregates expose their
  actual `telemetry_aggregate` source and stored quality, not an invented device
  source. Their single stored quality cannot reconstruct multiple original flags.
- Live and HA observations preserve simulated and stale flags simultaneously.
  Source freshness is separate from simulation quality. Future telemetry beyond
  the 30-second evaluation tolerance is stale. HA expired/unavailable values are
  null; original quality, mapping version, configured/normalized units and sample
  timestamps remain visible. `physical_control` and `control_authorized` are false.
- Forecast energy integrates the latest eligible expected forecast, including
  the remaining portion of the current interval. Weather/forecast simulation and
  forecasts older than six hours remain flagged. Coverage reports the fraction
  of the next 24 hours actually forecast; missing periods are not extrapolated.
- Costs reuse historical hourly dashboard prices and energy, with Decimal
  arithmetic. Coverage uses the weakest contributing metric's coverage, weighted
  by elapsed seconds. Missing energy or required prices exclude that hour.
  These are estimates, explicitly excluding fixed fees, monthly netting and an
  invoice projection. They do not implement a second billing formula.

## Errors and cache

Errors are `{ "schema_version": 1, "error": { "code": "...", "message": "...",
"resource": null, "retry_after_seconds": null } }`. Branch on `code`, not wording.
401: `unauthenticated`/`reauth_required`; 403: `forbidden`; 404: `no_station`;
409: `station_required`; 422: `invalid_request`/`invalid_cursor`;
429: `rate_limited`; 503: `service_unavailable` when the rate-limit store fails.
Integration failures do not discard usable station data: overview/context return
200 with typed `issues` (`provider_disconnected`, `integration_disconnected`,
`reauth_required`, `stale`) alongside the affected resource.

Overview/charts send `Cache-Control: private, no-cache, max-age=0, must-revalidate`,
`Vary: Cookie, Authorization`, and a user-bound representation ETag. Reuse the ETag
with `If-None-Match`; authentication and authorization run **before every 304**.
Overview evaluation is quantized to 30 seconds; database changes within that
window invalidate the ETag immediately. There is no server/shared response cache.
Revalidation saves transfer bytes, not computation; poll at most every 30 seconds
while visible. The authenticated read budget is 120 requests/user/minute across
these endpoints; 429 includes `Retry-After`.

`Last-Modified` is the overview evaluation instant or latest chart aggregate
update (omitted for empty charts). It is informational; `If-Modified-Since` alone
never produces 304 because access and integration state can change independently.
Context, paginated history and errors use `no-store`.

## Generated clients and compatibility

`mobile-v1.openapi.json` is a reachable-schema subset of `/api/openapi.json`;
`mobile-v1.d.ts` is generated with the pinned `openapi-typescript` dev dependency.
Use the `paths` and `components` types in the companion app; no client application
implementation belongs in this repository.

```sh
python -m scripts.export_mobile_contract
npm run mobile:types
python -m scripts.export_mobile_contract --check
npm run mobile:types:check
pytest tests/unit/test_mobile_contract.py tests/integration/test_mobile_api.py -q
```

CI checks schema/type drift and the frozen initial v1 compatibility signatures in
`tests/fixtures/mobile-v1-compatibility.json`. Do not regenerate that baseline to
silence a breaking change. Existing types, nullability, auth and required inputs
must remain compatible; breaking changes need a new major URL. Additive fields,
optional parameters and endpoints are allowed. Consumers should tolerate new
object keys. Update the generated artifacts in the same PR as an additive change.

The PostgreSQL integration suite exercises cookie/RBAC/tenant boundaries, cache
reauthorization, cursor isolation, DST/leap dates, NULL/zero, carry-in provenance,
HA redaction and provider isolation. The first-screen smoke load adds 35 devices
and notifications, performs eight warm reads, and checks constant query count,
payload under 40 KB, and maximum latency under two seconds. This is a local/CI
regression budget, not a production concurrency benchmark or hardware test.
The fixture also includes 169 hourly energy aggregates, historical tariffs and
24 forecast intervals. On the development PostgreSQL run, the response was
12,402 bytes, the mean query count was 41.125 (including session authorization),
and the slowest of eight warm reads took 99.4 ms. Results depend on the runner.
No schema migration or backfill is required.
