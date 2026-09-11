# Repository guidance

Read docs/CODE_STANDARDS.md and the relevant API/schema before changes. Keep changes scoped to the issue; no global reformatting. Use FastAPI services, SQLAlchemy, Alembic and the existing UI.

## Data and compatibility
- Unknown is not zero. Preserve NULL and per-metric coverage through API, dashboard, forecast and export. Explicit zero limits/SOC remain valid.
- Changing column nullability or a service contract requires updating all callers in the same PR, with integration regressions.
- Propagate simulated/stale provenance through every contributing segment, including carry-in. Never present synthetic inputs as measured facts.
- Store timezone-aware UTC instants; derive day/month boundaries from the station or market calendar. Test DST and leap years.
- Changing aggregation keys/calendars requires a populated-database migration strategy. Preserve legacy history separately; never sum overlapping generations.
- Downgrade tests must assert retained values, including mixed NULL/non-NULL rows. Do not zero valid data during rollback.
- Keep energy and money in Decimal in domain/storage code; convert only at explicit presentation/solver boundaries.

## Device control and security
- Keep desired configuration, reported observations, command ACK and verified application distinct. Saving a form is not evidence of hardware application.
- Device IDs and serials are inventory, not tenant authorization. Enforce station/device scope, RBAC, CSRF for cookie-authenticated writes, and audit.
- Validate finite values, versions, model/firmware compatibility, allowlists and expiry before commands. Never invent DEYE registers or advertise untested hardware support.
- No physical writes from web request handlers. Read-only agents remain read-only; capabilities must be explicitly reported.
- Retry identity and immutable snapshots must survive retries. Use PostgreSQL constraints/locks for concurrent mutations, not in-memory checks alone.

## Validation and delivery
- Run ruff check and relevant tests. Migration/concurrency tests use PostgreSQL; HTTP integrations are deterministic with mocks in CI.
- Add behavioral regressions for confirmed bugs, not tests that simply mirror implementation.
- Check CI on the actual updated head before an authorized merge. Do not bypass failed checks or branch rules.
- Report local, CI, end-to-end and hardware verification separately and honestly.
