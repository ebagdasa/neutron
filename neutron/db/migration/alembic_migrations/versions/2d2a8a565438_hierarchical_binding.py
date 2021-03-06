# Copyright 2014 OpenStack Foundation
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#

"""ML2 hierarchical binding

Revision ID: 2d2a8a565438
Revises: 408cfbf6923c
Create Date: 2014-08-24 21:56:36.422885

"""

# revision identifiers, used by Alembic.
revision = '2d2a8a565438'
down_revision = '408cfbf6923c'

from alembic import op
import sqlalchemy as sa

fk_names = {
    'mysql': {
        'ml2_port_bindings': {
            'segment': 'ml2_port_bindings_ibfk_2'
        },
        'ml2_dvr_port_bindings': {
            'segment': 'ml2_dvr_port_bindings_ibfk_2'
        },
    },
    'postgresql': {
        'ml2_port_bindings': {
            'segment': 'ml2_port_bindings_segment_fkey'
        },
        'ml2_dvr_port_bindings': {
            'segment': 'ml2_dvr_port_bindings_segment_fkey'
        },
    },
}

port_binding_tables = ['ml2_port_bindings', 'ml2_dvr_port_bindings']


def upgrade(active_plugins=None, options=None):

    context = op.get_context()
    dialect = context.bind.dialect.name

    op.create_table(
        'ml2_port_binding_levels',
        sa.Column('port_id', sa.String(length=36), nullable=False),
        sa.Column('host', sa.String(length=255), nullable=False),
        sa.Column('level', sa.Integer(), autoincrement=False, nullable=False),
        sa.Column('driver', sa.String(length=64), nullable=True),
        sa.Column('segment_id', sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(['port_id'], ['ports.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['segment_id'], ['ml2_network_segments.id'],
                                ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('port_id', 'host', 'level')
    )

    for table in port_binding_tables:
        op.execute((
            "INSERT INTO ml2_port_binding_levels "
            "SELECT port_id, host, 0 AS level, driver, segment AS segment_id "
            "FROM %s "
            "WHERE host <> '' "
            "AND driver <> '';"
        ) % table)

    op.drop_constraint(fk_names[dialect]['ml2_dvr_port_bindings']['segment'],
                       'ml2_dvr_port_bindings', 'foreignkey')
    op.drop_column('ml2_dvr_port_bindings', 'cap_port_filter')
    op.drop_column('ml2_dvr_port_bindings', 'segment')
    op.drop_column('ml2_dvr_port_bindings', 'driver')

    op.drop_constraint(fk_names[dialect]['ml2_port_bindings']['segment'],
                       'ml2_port_bindings', 'foreignkey')
    op.drop_column('ml2_port_bindings', 'driver')
    op.drop_column('ml2_port_bindings', 'segment')


def downgrade(active_plugins=None, options=None):

    context = op.get_context()
    dialect = context.bind.dialect.name

    op.add_column('ml2_port_bindings',
                  sa.Column('segment', sa.String(length=36), nullable=True))
    op.add_column('ml2_port_bindings',
                  sa.Column('driver', sa.String(length=64), nullable=True))
    op.create_foreign_key(
        fk_names[dialect]['ml2_port_bindings']['segment'],
        source='ml2_port_bindings', referent='ml2_network_segments',
        local_cols=['segment'], remote_cols=['id'], ondelete='SET NULL'
    )

    op.add_column('ml2_dvr_port_bindings',
                  sa.Column('driver', sa.String(length=64), nullable=True))
    op.add_column('ml2_dvr_port_bindings',
                  sa.Column('segment', sa.String(length=36), nullable=True))
    op.add_column('ml2_dvr_port_bindings',
                  sa.Column('cap_port_filter', sa.Boolean, nullable=False))
    op.create_foreign_key(
        fk_names[dialect]['ml2_dvr_port_bindings']['segment'],
        source='ml2_dvr_port_bindings', referent='ml2_network_segments',
        local_cols=['segment'], remote_cols=['id'], ondelete='SET NULL'
    )

    for table in port_binding_tables:
        if dialect == 'postgresql':
            op.execute((
                "UPDATE %s pb "
                "SET driver = pbl.driver, segment = pbl.segment_id "
                "FROM ml2_port_binding_levels pbl "
                "WHERE pb.port_id = pbl.port_id "
                "AND pb.host = pbl.host "
                "AND pbl.level = 0;"
            ) % table)
        else:
            op.execute((
                "UPDATE %s pb "
                "INNER JOIN ml2_port_binding_levels pbl "
                "ON pb.port_id = pbl.port_id "
                "AND pb.host = pbl.host "
                "AND pbl.level = 0 "
                "SET pb.driver = pbl.driver, pb.segment = pbl.segment_id;"
            ) % table)

    op.drop_table('ml2_port_binding_levels')
