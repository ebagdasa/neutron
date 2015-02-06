# Copyright 2013 OpenStack Foundation
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

"""
ML2 Mechanism Driver for Cisco Nexus platforms.
"""

from oslo.config import cfg

from neutron.common import constants as n_const
from neutron.db import api as db_api
from neutron.extensions import portbindings
from neutron.openstack.common.gettextutils import _LW
from neutron.openstack.common import lockutils
from neutron.openstack.common import log as logging
from neutron.plugins.common import constants as p_const
from neutron.plugins.ml2 import db as ml2_db
from neutron.plugins.ml2 import driver_api as api
from neutron.plugins.ml2.drivers.cisco.nexus import config as conf
from neutron.plugins.ml2.drivers.cisco.nexus import constants as const
from neutron.plugins.ml2.drivers.cisco.nexus import exceptions as excep
from neutron.plugins.ml2.drivers.cisco.nexus import nexus_db_v2 as nxos_db
from neutron.plugins.ml2.drivers.cisco.nexus import nexus_network_driver

LOG = logging.getLogger(__name__)


class CiscoNexusMechanismDriver(api.MechanismDriver):

    """Cisco Nexus ML2 Mechanism Driver."""

    def initialize(self):
        # Create ML2 device dictionary from ml2_conf.ini entries.
        conf.ML2MechCiscoConfig()

        # Extract configuration parameters from the configuration file.
        self._nexus_switches = conf.ML2MechCiscoConfig.nexus_dict
        LOG.debug(_("nexus_switches found = %s"), self._nexus_switches)

        self.driver = nexus_network_driver.CiscoNexusDriver()

    def _valid_network_segment(self, segment):
        return (cfg.CONF.ml2_cisco.managed_physical_network is None or
                cfg.CONF.ml2_cisco.managed_physical_network ==
                segment[api.PHYSICAL_NETWORK])

    def _get_vlan_info(self, segment):
        if (segment and segment[api.NETWORK_TYPE] == p_const.TYPE_VLAN and
            self._valid_network_segment(segment)):
            return (segment.get(api.SEGMENTATION_ID),
                    segment.get(api.PROVIDER_SEGMENT))
        else:
            return None, None

    def _is_supported_deviceowner(self, port):
        return (port['device_owner'].startswith('compute') or
                port['device_owner'] == n_const.DEVICE_OWNER_DHCP)

    def _is_status_active(self, port):
        return port['status'] == n_const.PORT_STATUS_ACTIVE

    def _get_switch_info(self, host_id):
        host_connections = []
        for switch_ip, attr in self._nexus_switches:
            if str(attr) == str(host_id):
                for port_id in (
                    self._nexus_switches[switch_ip, attr].split(',')):
                    if ':' in port_id:
                        intf_type, port = port_id.split(':')
                    else:
                        intf_type, port = 'ethernet', port_id
                    host_connections.append((switch_ip, intf_type, port))

        if host_connections:
            return host_connections
        else:
            raise excep.NexusComputeHostNotConfigured(host=host_id)

    def _get_switch_nve_info(self, host_id):
        host_nve_connections = []
        for switch_ip, attr in self._nexus_switches:
            if str(attr) == str(host_id):
                host_nve_connections.append(switch_ip)

        if host_nve_connections:
            return host_nve_connections
        else:
            raise excep.NexusComputeHostNotConfigured(host=host_id)

    def _configure_nve_db(self, vni, device_id, mcast_group, host_id):
        """Create the nexus NVE database entry.

        Called during update precommit port event.
        """
        host_nve_connections = self._get_switch_nve_info(host_id)
        for switch_ip in host_nve_connections:
            if not nxos_db.get_nve_vni_member_bindings(vni, switch_ip,
                                                       device_id):
                nxos_db.add_nexusnve_binding(vni, switch_ip, device_id,
                                             mcast_group)

    def _configure_nve_member(self, vni, device_id, mcast_group, host_id):
        """Add "member vni" configuration to the NVE interface.

        Called during update postcommit port event.
        """
        host_nve_connections = self._get_switch_nve_info(host_id)

        for switch_ip in host_nve_connections:
            # If configured to set global VXLAN values then
            #   If this is the first database entry for this switch_ip
            #   then configure the "interface nve" entry on the switch.
            if cfg.CONF.ml2_cisco.vxlan_global_config:
                nve_bindings = nxos_db.get_nve_switch_bindings(switch_ip)
                if len(nve_bindings) == 1:
                    LOG.debug("Nexus: create NVE interface")
                    loopback = self._nexus_switches.get(
                                        (switch_ip, 'nve_src_intf'), '0')
                    self.driver.enable_vxlan_feature(switch_ip,
                                                 const.NVE_INT_NUM, loopback)

            # If this is the first database entry for this (VNI, switch_ip)
            # then configure the "member vni #" entry on the switch.
            member_bindings = nxos_db.get_nve_vni_switch_bindings(vni,
                                                                  switch_ip)
            if len(member_bindings) == 1:
                LOG.debug("Nexus: add member")
                self.driver.create_nve_member(switch_ip, const.NVE_INT_NUM,
                                              vni, mcast_group)

    def _delete_nve_db(self, vni, device_id, mcast_group, host_id):
        """Delete the nexus NVE database entry.

        Called during delete precommit port event.
        """
        rows = nxos_db.get_nve_vni_deviceid_bindings(vni, device_id)
        for row in rows:
            nxos_db.remove_nexusnve_binding(vni, row.switch_ip, device_id)

    def _delete_nve_member(self, vni, device_id, mcast_group, host_id):
        """Remove "member vni" configuration from the NVE interface.

        Called during delete postcommit port event.
        """
        host_nve_connections = self._get_switch_nve_info(host_id)
        for switch_ip in host_nve_connections:
            if not nxos_db.get_nve_vni_switch_bindings(vni, switch_ip):
                self.driver.delete_nve_member(switch_ip, const.NVE_INT_NUM,
                                              vni)
            if (cfg.CONF.ml2_cisco.vxlan_global_config and
                not nxos_db.get_nve_switch_bindings(switch_ip)):
                self.driver.disable_vxlan_feature(switch_ip)

    def _configure_nxos_db(self, vlan_id, device_id, host_id, vni,
                           is_provider_vlan):
        """Create the nexus database entry.

        Called during update precommit port event.
        """
        host_connections = self._get_switch_info(host_id)
        for switch_ip, intf_type, nexus_port in host_connections:
            port_id = '%s:%s' % (intf_type, nexus_port)
            nxos_db.add_nexusport_binding(port_id, str(vlan_id), str(vni),
                                          switch_ip, device_id)

    def _configure_switch_entry(self, vlan_id, device_id, host_id, vni,
                                is_provider_vlan):
        """Create a nexus switch entry.

        if needed, create a VLAN in the appropriate switch/port and
        configure the appropriate interfaces for this VLAN.

        Called during update postcommit port event.
        """
        host_connections = self._get_switch_info(host_id)
        if is_provider_vlan:
            vlan_name = cfg.CONF.ml2_cisco.provider_vlan_name_prefix
            auto_create = cfg.CONF.ml2_cisco.provider_vlan_auto_create
            auto_trunk = cfg.CONF.ml2_cisco.provider_vlan_auto_trunk
        else:
            vlan_name = cfg.CONF.ml2_cisco.vlan_name_prefix
            auto_create = True
            auto_trunk = True
        vlan_name_max_len = const.NEXUS_MAX_VLAN_NAME_LEN - len(str(vlan_id))
        if len(vlan_name) > vlan_name_max_len:
            vlan_name = vlan_name[:vlan_name_max_len]
            LOG.warn(_LW("Nexus: truncating vlan name to %s"), vlan_name)
        vlan_name = vlan_name + str(vlan_id)

        # (nexus_port,switch_ip) will be unique in each iteration.
        # But switch_ip will repeat if host has >1 connection to same switch.
        # So track which switch_ips already have vlan created in this loop.
        vlan_already_created = []
        for switch_ip, intf_type, nexus_port in host_connections:

            # The VLAN needs to be created on the switch if no other
            # instance has been placed in this VLAN on a different host
            # attached to this switch.  Search the existing bindings in the
            # database.  If all the instance_id in the database match the
            # current device_id, then create the VLAN, but only once per
            # switch_ip.  Otherwise, just trunk.
            all_bindings = nxos_db.get_nexusvlan_binding(vlan_id, switch_ip)
            previous_bindings = [row for row in all_bindings
                    if row.instance_id != device_id]
            if previous_bindings or (switch_ip in vlan_already_created):
                if auto_trunk:
                    LOG.debug("Nexus: trunk vlan %s"), vlan_name
                    self.driver.enable_vlan_on_trunk_int(switch_ip, vlan_id,
                                                         intf_type, nexus_port)
            else:
                vlan_already_created.append(switch_ip)
                if auto_create and auto_trunk:
                    LOG.debug("Nexus: create & trunk vlan %s"), vlan_name
                    self.driver.create_and_trunk_vlan(
                        switch_ip, vlan_id, vlan_name, intf_type, nexus_port,
                        vni)
                elif auto_create:
                    LOG.debug("Nexus: create vlan %s"), vlan_name
                    self.driver.create_vlan(switch_ip, vlan_id, vlan_name, vni)
                elif auto_trunk:
                    LOG.debug("Nexus: trunk vlan %s"), vlan_name
                    self.driver.enable_vlan_on_trunk_int(switch_ip, vlan_id,
                        intf_type, nexus_port)

    def _delete_nxos_db(self, vlan_id, device_id, host_id, vni,
                        is_provider_vlan):
        """Delete the nexus database entry.

        Called during delete precommit port event.
        """
        try:
            rows = nxos_db.get_nexusvm_bindings(vlan_id, device_id)
            for row in rows:
                nxos_db.remove_nexusport_binding(row.port_id, row.vlan_id,
                                    row.vni, row.switch_ip, row.instance_id)
        except excep.NexusPortBindingNotFound:
            return

    def _delete_switch_entry(self, vlan_id, device_id, host_id, vni,
                             is_provider_vlan):
        """Delete the nexus switch entry.

        By accessing the current db entries determine if switch
        configuration can be removed.

        Called during delete postcommit port event.
        """
        host_connections = self._get_switch_info(host_id)

        # (nexus_port,switch_ip) will be unique in each iteration.
        # But switch_ip will repeat if host has >1 connection to same switch.
        # So track which switch_ips already have vlan removed in this loop.
        vlan_already_removed = []
        for switch_ip, intf_type, nexus_port in host_connections:

            # if there are no remaining db entries using this vlan on this
            # nexus switch port then remove vlan from the switchport trunk.
            port_id = '%s:%s' % (intf_type, nexus_port)
            auto_create = True
            auto_trunk = True
            if is_provider_vlan:
                auto_create = cfg.CONF.ml2_cisco.provider_vlan_auto_create
                auto_trunk = cfg.CONF.ml2_cisco.provider_vlan_auto_trunk
            try:
                nxos_db.get_port_vlan_switch_binding(port_id, vlan_id,
                                                     switch_ip)
            except excep.NexusPortBindingNotFound:
                if auto_trunk:
                    self.driver.disable_vlan_on_trunk_int(switch_ip, vlan_id,
                        intf_type, nexus_port)

                # if there are no remaining db entries using this vlan on this
                # nexus switch then remove the vlan.
                if auto_create:
                    try:
                        nxos_db.get_nexusvlan_binding(vlan_id, switch_ip)
                    except excep.NexusPortBindingNotFound:

                        # Do not perform a second time on same switch
                        if switch_ip not in vlan_already_removed:
                            self.driver.delete_vlan(switch_ip, vlan_id)
                            vlan_already_removed.append(switch_ip)

    def _is_segment_nexus_vxlan(self, segment):
        return segment[api.NETWORK_TYPE] == p_const.TYPE_NEXUS_VXLAN

    def _get_segments(self, top_segment, bottom_segment):
        # Return vlan segment and vxlan segment (if configured).
        if top_segment is None:
            return None, None
        elif self._is_segment_nexus_vxlan(top_segment):
            return bottom_segment, top_segment
        else:
            return top_segment, None

    def _is_vm_migrating(self, context, vlan_segment, orig_vlan_segment):
        if not vlan_segment and orig_vlan_segment:
            return (context.current.get(portbindings.HOST_ID) !=
                    context.original.get(portbindings.HOST_ID))

    def _log_missing_fields(self):
        LOG.warn(_LW("Nexus: Segment is None, Event not processed."))

    def _port_action_vlan(self, port, segment, func, vni):
        """Verify configuration and then process event."""

        # If the segment is None, just log a warning message and return.
        if segment is None:
            self._log_missing_fields()
            return

        device_id = port.get('device_id')
        host_id = port.get(portbindings.HOST_ID)
        vlan_id, is_provider = self._get_vlan_info(segment)
        settings = {"vlan_id": vlan_id,
                    "device_id": device_id,
                    "host_id": host_id,
                    "is_provider": is_provider is not None}
        missing_fields = [field for field, value in settings.items()
                          if not value]
        if not missing_fields:
            func(vlan_id, device_id, host_id, vni, is_provider)
        else:
            raise excep.NexusMissingRequiredFields(
                fields=' '.join(missing_fields))

    def _port_action_vxlan(self, port, segment, func):
        """Verify configuration and then process event."""

        # If the segment is None, just log a warning message and return.
        if segment is None:
            self._log_missing_fields()
            return

        device_id = port.get('device_id')
        mcast_group = segment.get(api.PHYSICAL_NETWORK)
        host_id = port.get(portbindings.HOST_ID)
        vni = segment.get(api.SEGMENTATION_ID)

        if vni and device_id and mcast_group and host_id:
            func(vni, device_id, mcast_group, host_id)
            return vni
        else:
            fields = "vni " if not vni else ""
            fields += "device_id " if not device_id else ""
            fields += "mcast_group " if not mcast_group else ""
            fields += "host_id" if not host_id else ""
            raise excep.NexusMissingRequiredFields(fields=fields)

    @lockutils.synchronized('cisco-nexus-portlock')
    def update_port_precommit(self, context):
        """Update port pre-database transaction commit event."""
        vlan_segment, vxlan_segment = self._get_segments(
                                        context.top_bound_segment,
                                        context.bottom_bound_segment)
        orig_vlan_segment, orig_vxlan_segment = self._get_segments(
                                        context.original_top_bound_segment,
                                        context.original_bottom_bound_segment)

        # if VM migration is occurring then remove previous database entry
        # else process update event.
        if self._is_vm_migrating(context, vlan_segment, orig_vlan_segment):
            vni = self._port_action_vxlan(context.original, orig_vxlan_segment,
                        self._delete_nve_db) if orig_vxlan_segment else 0
            self._port_action_vlan(context.original, orig_vlan_segment,
                                   self._delete_nxos_db, vni)
        else:
            if (self._is_supported_deviceowner(context.current) and
                self._is_status_active(context.current)):
                vni = self._port_action_vxlan(context.current, vxlan_segment,
                            self._configure_nve_db) if vxlan_segment else 0
                self._port_action_vlan(context.current, vlan_segment,
                                       self._configure_nxos_db, vni)

    @lockutils.synchronized('cisco-nexus-portlock')
    def update_port_postcommit(self, context):
        """Update port non-database commit event."""
        vlan_segment, vxlan_segment = self._get_segments(
                                        context.top_bound_segment,
                                        context.bottom_bound_segment)
        orig_vlan_segment, orig_vxlan_segment = self._get_segments(
                                        context.original_top_bound_segment,
                                        context.original_bottom_bound_segment)

        # if VM migration is occurring then remove previous nexus switch entry
        # else process update event.
        if self._is_vm_migrating(context, vlan_segment, orig_vlan_segment):
            vni = self._port_action_vxlan(context.original, orig_vxlan_segment,
                        self._delete_nve_member) if orig_vxlan_segment else 0
            self._port_action_vlan(context.original, orig_vlan_segment,
                                   self._delete_switch_entry, vni)
        else:
            if (self._is_supported_deviceowner(context.current) and
                self._is_status_active(context.current)):
                vni = self._port_action_vxlan(context.current, vxlan_segment,
                            self._configure_nve_member) if vxlan_segment else 0
                self._port_action_vlan(context.current, vlan_segment,
                                       self._configure_switch_entry, vni)

    @lockutils.synchronized('cisco-nexus-portlock')
    def delete_port_precommit(self, context):
        """Delete port pre-database commit event."""
        if self._is_supported_deviceowner(context.current):
            vlan_segment, vxlan_segment = self._get_segments(
                                                context.top_bound_segment,
                                                context.bottom_bound_segment)
            vni = self._port_action_vxlan(context.current, vxlan_segment,
                             self._delete_nve_db) if vxlan_segment else 0
            self._port_action_vlan(context.current, vlan_segment,
                                   self._delete_nxos_db, vni)

    @lockutils.synchronized('cisco-nexus-portlock')
    def delete_port_postcommit(self, context):
        """Delete port non-database commit event."""
        if self._is_supported_deviceowner(context.current):
            vlan_segment, vxlan_segment = self._get_segments(
                                                context.top_bound_segment,
                                                context.bottom_bound_segment)
            vni = self._port_action_vxlan(context.current, vxlan_segment,
                             self._delete_nve_member) if vxlan_segment else 0
            self._port_action_vlan(context.current, vlan_segment,
                                   self._delete_switch_entry, vni)

    def bind_port(self, context):
        LOG.debug("Attempting to bind port %(port)s on network %(network)s",
                  {'port': context.current['id'],
                   'network': context.network.current['id']})
        for segment in context.segments_to_bind:
            if self._is_segment_nexus_vxlan(segment):

                # Find physical network setting for this host.
                host_id = context.current.get(portbindings.HOST_ID)
                host_connections = self._get_switch_info(host_id)
                for switch_ip, attr2, attr3 in host_connections:
                    physnet = self._nexus_switches.get((switch_ip, 'physnet'))
                    if physnet:
                        break
                else:
                    raise excep.PhysnetNotConfigured(host_id=host_id,
                                             host_connections=host_connections)

                # Allocate dynamic vlan segment.
                vlan_segment = {api.NETWORK_TYPE: p_const.TYPE_VLAN,
                                api.PHYSICAL_NETWORK: physnet}
                context.allocate_dynamic_segment(vlan_segment)

                # Retrieve the dynamically allocated segment.
                # Database has provider_segment dictionary key.
                network_id = context.current['network_id']
                dynamic_segment = ml2_db.get_dynamic_segment(
                                    db_api.get_session(), network_id, physnet)

                # Have other drivers bind the VLAN dynamic segment.
                if dynamic_segment:
                    context.continue_binding(segment[api.ID],
                                             [dynamic_segment])
                else:
                    raise excep.NoDynamicSegmentAllocated(
                                        network_id=network_id, physnet=physnet)
            else:
                LOG.debug("No binding required for segment ID %(id)s, "
                          "segment %(seg)s, phys net %(physnet)s, and "
                          "network type %(nettype)s",
                          {'id': segment[api.ID],
                           'seg': segment[api.SEGMENTATION_ID],
                           'physnet': segment[api.PHYSICAL_NETWORK],
                           'nettype': segment[api.NETWORK_TYPE]})
