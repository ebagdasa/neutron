# Copyright 2015 OpenStack Foundation
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

"""Add is_provider_vlan to each port binding

Revision ID: 38b689a6b56b
Revises: 796c68dffbb
Create Date: 2015-02-03 16:51:27.244045

"""

# revision identifiers, used by Alembic.
revision = '38b689a6b56b'
down_revision = '796c68dffbb'

from alembic import op
import sqlalchemy as sa


def upgrade():
    op.add_column('cisco_ml2_nexusport_bindings', sa.Column(
        'is_provider_vlan', sa.Boolean(), nullable=False,
        server_default=sa.sql.false()))


def downgrade():
    op.drop_column('cisco_ml2_nexusport_bindings', 'is_provider_vlan')
