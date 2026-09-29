# Mobile read API v1 (#199)

The platform owns the canonical read contract and calculations. Mobile UI and
native sessions remain in [EMS-mobile-app #10](https://github.com/bbogdan59/EMS-mobile-app/issues/10)
and [#8](https://github.com/bbogdan59/EMS-mobile-app/issues/8); the mobile umbrella
is [#23](https://github.com/bbogdan59/EMS-mobile-app/issues/23). These issues were
transferred from the platform roadmap. No client application code belongs here.

Expose additive `/api/v1/mobile` GET endpoints for overview, aggregate charts,
notifications, charging sessions and allowlisted Home Assistant context.
Use existing human session authentication and station authorization; device and
HA bridge bearer tokens never authorize human analytics. Native auth work must
plug into the same human authorization boundary, not reinterpret device tokens.

Reuse dashboard KPI/cost/forecast calculations and retained aggregates. Add exact
Decimal presentation options to shared services without changing web defaults.
Do not fetch providers, initiate collection or write hardware in these handlers.
NULL, coverage and source quality remain separate, including simulated/stale
inputs. Legacy station aggregates cannot identify an individual hardware source;
their source is explicitly `telemetry_aggregate`.

The compact overview includes only integration summaries. Entity details and
paged histories load separately. Queries are bounded and independent of the
number of child records; pagination uses signed, expiring cursors bound to the
user, station, collection and filters. No cross-tenant/shared HTTP response cache.

Every conditional GET reauthorizes the current session and membership before
ETag validation. Overview evaluation uses 30-second windows; persisted changes
within a window change the representation hash immediately. Cache policy is
private, must revalidate; errors and sensitive context/history use no-store.
Last-Modified is informational; only ETag supports conditional 304 responses.

Publish a reproducible OpenAPI subset and generated TypeScript declarations
under `contracts/mobile/`, with CI drift and compatibility tests. Roll out the
backward-compatible platform contract before mobile consumers. No database
aggregation keys or stored history are changed; no migration is required.
