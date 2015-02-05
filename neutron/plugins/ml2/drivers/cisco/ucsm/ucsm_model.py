# Copyright 2014 OpenStack Foundation
# All rights reserved.
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

import sqlalchemy as sa

from neutron.db import api as db_api
from neutron.db import model_base


class PortProfile(model_base.BASEV2):

    """Port profiles created on the UCS Manager."""

    __tablename__ = 'ml2_ucsm_port_profiles'

    vlan_id = sa.Column(sa.Integer(), nullable=False, primary_key=True)
    profile_id = sa.Column(sa.String(64), nullable=False)
    created_on_ucs = sa.Column(sa.Boolean(), nullable=False)


class UcsmDbModel(object):
    def __init__(self):
        self.session = db_api.get_session()

    def is_port_profile_created(self, vlan_id):
        """Returns True if port profile has been created on UCS Manager."""
        entry = self.session.query(PortProfile).filter_by(
            vlan_id=vlan_id).first()
        return True if (entry and entry.created_on_ucs) else False

    def get_port_profile_for_vlan(self, vlan_id):
        """Returns Vlan id associated with the port profile."""
        entry = self.session.query(PortProfile).filter_by(
            vlan_id=vlan_id).first()
        return entry.profile_id if entry else None

    def add_port_profile(self, profile_name, vlan_id):
        """Adds a port profile and its vlan_id to the table."""
        port_profile = PortProfile(profile_id=profile_name,
                                   vlan_id=vlan_id,
                                   created_on_ucs=False)
        with self.session.begin(subtransactions=True):
            self.session.add(port_profile)
        return port_profile

    def set_port_profile_created(self, vlan_id, profile_name):
        """Sets created_on_ucs flag to True."""
        with self.session.begin(subtransactions=True):
            port_profile = self.session.query(PortProfile).filter_by(
                vlan_id=vlan_id, profile_id=profile_name).first()
            if port_profile:
                port_profile.created_on_ucs = True
                self.session.merge(port_profile)
            else:
                new_profile = PortProfile(profile_id=profile_name,
                                          vlan_id=vlan_id,
                                          created_on_ucs=True)
                self.session.add(new_profile)