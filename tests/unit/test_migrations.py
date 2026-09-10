from __future__ import annotations

from sqlalchemy import inspect

from app.database import Base
import app.models  # noqa: F401


def test_all_model_tables_exist_after_migration(engine):
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    expected_tables = set(Base.metadata.tables.keys())
    missing = expected_tables - existing_tables
    assert not missing, f"Tabele lipsa dupa migrare: {missing}"


def test_key_unique_constraints_present(engine):
    inspector = inspect(engine)
    telemetry_uniques = inspector.get_unique_constraints("telemetry_raw")
    columns_sets = [set(uc["column_names"]) for uc in telemetry_uniques]
    assert {"device_id", "boot_id", "sequence"} in columns_sets

    command_uniques = inspector.get_unique_constraints("commands")
    columns_sets = [set(uc["column_names"]) for uc in command_uniques]
    assert {"device_id", "idempotency_key"} in columns_sets
