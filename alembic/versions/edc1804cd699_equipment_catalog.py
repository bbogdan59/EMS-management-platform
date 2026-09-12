"""equipment catalog

Revision ID: edc1804cd699
Revises: e2a4c8f1d9b3
Create Date: 2026-09-12T14:14:28.000000

Part of issue #42: catalog administrabil de invertoare/baterii/panouri
(`EquipmentManufacturer`/`EquipmentModel`), cu versionare de specificatii
(`spec_revision`) si activare/dezactivare nedistructiva.

`StationConfigVersion`/`PanelGroup` primesc o referinta optionala catre
`EquipmentModel` PLUS un snapshot (`*_model_snapshot`/`*_model_spec_revision`)
capturat la momentul selectiei -- o editare ulterioara a catalogului nu
modifica retroactiv configuratii deja publicate. `*_custom_label` acopera
declararea unui echipament care lipseste din catalog (fara sa creeze o
inregistrare de catalog nevalidata). Toate coloanele noi sunt nullable --
configuratiile existente raman valide fara echipament de catalog asociat.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "edc1804cd699"
down_revision = "e2a4c8f1d9b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "equipment_manufacturers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "equipment_models",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("manufacturer_id", sa.Uuid(), nullable=False),
        sa.Column("equipment_type", sa.String(length=20), nullable=False),
        sa.Column("model_name", sa.String(length=200), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("spec_revision", sa.Integer(), nullable=False),
        sa.Column("specs", sa.JSON(), nullable=False),
        sa.Column("source_note", sa.String(length=500), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["manufacturer_id"], ["equipment_manufacturers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("manufacturer_id", "equipment_type", "model_name", name="uq_equipment_model_identity"),
    )
    op.create_index("ix_equipment_models_equipment_type", "equipment_models", ["equipment_type"])
    op.create_index("ix_equipment_models_manufacturer_id", "equipment_models", ["manufacturer_id"])

    op.add_column("station_config_versions", sa.Column("inverter_model_id", sa.Uuid(), nullable=True))
    op.add_column("station_config_versions", sa.Column("inverter_model_spec_revision", sa.Integer(), nullable=True))
    op.add_column("station_config_versions", sa.Column("inverter_model_snapshot", sa.JSON(), nullable=True))
    op.add_column("station_config_versions", sa.Column("inverter_custom_label", sa.String(length=200), nullable=True))
    op.add_column("station_config_versions", sa.Column("battery_model_id", sa.Uuid(), nullable=True))
    op.add_column("station_config_versions", sa.Column("battery_model_spec_revision", sa.Integer(), nullable=True))
    op.add_column("station_config_versions", sa.Column("battery_model_snapshot", sa.JSON(), nullable=True))
    op.add_column("station_config_versions", sa.Column("battery_custom_label", sa.String(length=200), nullable=True))
    op.create_foreign_key(
        "fk_station_config_versions_inverter_model_id", "station_config_versions", "equipment_models",
        ["inverter_model_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_station_config_versions_battery_model_id", "station_config_versions", "equipment_models",
        ["battery_model_id"], ["id"], ondelete="SET NULL",
    )

    op.add_column("panel_groups", sa.Column("pv_module_model_id", sa.Uuid(), nullable=True))
    op.add_column("panel_groups", sa.Column("pv_module_model_spec_revision", sa.Integer(), nullable=True))
    op.add_column("panel_groups", sa.Column("pv_module_model_snapshot", sa.JSON(), nullable=True))
    op.add_column("panel_groups", sa.Column("pv_module_custom_label", sa.String(length=200), nullable=True))
    op.add_column("panel_groups", sa.Column("pv_module_count", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_panel_groups_pv_module_model_id", "panel_groups", "equipment_models",
        ["pv_module_model_id"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_panel_groups_pv_module_model_id", "panel_groups", type_="foreignkey")
    op.drop_column("panel_groups", "pv_module_count")
    op.drop_column("panel_groups", "pv_module_custom_label")
    op.drop_column("panel_groups", "pv_module_model_snapshot")
    op.drop_column("panel_groups", "pv_module_model_spec_revision")
    op.drop_column("panel_groups", "pv_module_model_id")

    op.drop_constraint("fk_station_config_versions_battery_model_id", "station_config_versions", type_="foreignkey")
    op.drop_constraint("fk_station_config_versions_inverter_model_id", "station_config_versions", type_="foreignkey")
    op.drop_column("station_config_versions", "battery_custom_label")
    op.drop_column("station_config_versions", "battery_model_snapshot")
    op.drop_column("station_config_versions", "battery_model_spec_revision")
    op.drop_column("station_config_versions", "battery_model_id")
    op.drop_column("station_config_versions", "inverter_custom_label")
    op.drop_column("station_config_versions", "inverter_model_snapshot")
    op.drop_column("station_config_versions", "inverter_model_spec_revision")
    op.drop_column("station_config_versions", "inverter_model_id")

    op.drop_index("ix_equipment_models_manufacturer_id", table_name="equipment_models")
    op.drop_index("ix_equipment_models_equipment_type", table_name="equipment_models")
    op.drop_table("equipment_models")
    op.drop_table("equipment_manufacturers")
