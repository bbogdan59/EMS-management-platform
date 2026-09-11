"""Versioned inverter desired and reported state, issue #17."""
from alembic import op

revision = "d17a2c9f0311"
down_revision = "c91a17e4b208"
branch_labels = None
depends_on = None

def upgrade():
    op.execute('\nCREATE TABLE inverter_profiles (\n\tdigest VARCHAR(64) NOT NULL, \n\tdefinition JSON NOT NULL, \n\tapproved_by UUID NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (digest), \n\tFOREIGN KEY(approved_by) REFERENCES users (id)\n)\n\n')
    op.execute('\nCREATE TABLE inverter_desired (\n\tdevice_id UUID NOT NULL, \n\tprofile_id UUID NOT NULL, \n\tversion INTEGER NOT NULL, \n\trequest_id UUID NOT NULL, \n\trequest_hash VARCHAR(64) NOT NULL, \n\tdigest VARCHAR(64) NOT NULL, \n\tconnection JSON NOT NULL, \n\tsettings JSON NOT NULL, \n\treason VARCHAR(500) NOT NULL, \n\tcreated_by UUID NOT NULL, \n\tcommand_id UUID, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (device_id, version), \n\tUNIQUE (device_id, request_id), \n\tFOREIGN KEY(device_id) REFERENCES devices (id), \n\tFOREIGN KEY(profile_id) REFERENCES inverter_profiles (id), \n\tFOREIGN KEY(created_by) REFERENCES users (id), \n\tFOREIGN KEY(command_id) REFERENCES commands (id)\n)\n\n')
    op.execute('CREATE INDEX ix_inverter_desired_device_id ON inverter_desired (device_id)')
    op.execute('\nCREATE TABLE inverter_reports (\n\tdevice_id UUID NOT NULL, \n\tprofile_id UUID NOT NULL, \n\tversion INTEGER NOT NULL, \n\trequest_id UUID NOT NULL, \n\trequest_hash VARCHAR(64) NOT NULL, \n\tdesired_version INTEGER NOT NULL, \n\tdigest VARCHAR(64) NOT NULL, \n\tsnapshot JSON NOT NULL, \n\tmeasured_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\toutcome VARCHAR(16) NOT NULL, \n\treason VARCHAR(500), \n\tmodel VARCHAR(100) NOT NULL, \n\tfirmware VARCHAR(100) NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (device_id, version), \n\tUNIQUE (device_id, request_id), \n\tFOREIGN KEY(device_id) REFERENCES devices (id), \n\tFOREIGN KEY(profile_id) REFERENCES inverter_profiles (id)\n)\n\n')
    op.execute('CREATE INDEX ix_inverter_reports_device_id ON inverter_reports (device_id)')

def downgrade():
    op.drop_table("inverter_reports")
    op.drop_table("inverter_desired")
    op.drop_table("inverter_profiles")
