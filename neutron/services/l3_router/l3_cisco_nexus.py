# Copyright (c) 2014 Cisco Systems Inc.
# All Rights Reserved.
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

from oslo.config import cfg

from neutron.db import db_base_plugin_v2
from neutron.db import extraroute_db
from neutron.db import l3_gwmode_db
from neutron.extensions import providernet as provider
from neutron.openstack.common import excutils
from neutron.openstack.common import log as logging
from neutron.plugins.common import constants
from neutron.plugins.ml2.drivers.cisco.nexus import config as conf
from neutron.plugins.ml2.drivers.cisco.nexus import exceptions as cexc
from neutron.plugins.ml2.drivers.cisco.nexus import nexus_db_v2 as nxos_db
from neutron.plugins.ml2.drivers.cisco.nexus import nexus_network_driver

LOG = logging.getLogger(__name__)


class CiscoNexusL3ServicePlugin(db_base_plugin_v2.NeutronDbPluginV2,
                                extraroute_db.ExtraRoute_db_mixin,
                                l3_gwmode_db.L3_NAT_db_mixin):
    """Implementation of the Cisco Nexus L3 Router Service Plugin.

    This class implements an L3 service plugin that provides SVI
    configuration for Cisco Nexus switches.
    """
    supported_extension_aliases = ["router", "ext-gw-mode", "extraroute"]

    def __init__(self):
        super(CiscoNexusL3ServicePlugin, self).__init__()
        self.driver = nexus_network_driver.CiscoNexusDriver()

    @staticmethod
    def get_plugin_type():
        return constants.L3_ROUTER_NAT

    @staticmethod
    def get_plugin_description():
        """Returns string description of the plugin."""
        return _("L3 Router Service Plugin for basic L3 Cisco Nexus services.")

    def _get_vlanid(self, context, subnet):
        return(self._core_plugin.get_network(context,
                            subnet['network_id'])[provider.SEGMENTATION_ID])

    def _find_switch_for_svi(self):
        """Get a switch to create the SVI on."""
        LOG.debug("Find a switch for SVI.")

        nexus_switches = conf.ML2MechCiscoConfig.nexus_dict
        if nexus_switches:
            switch_dict = (dict((switch_ip, 0)
                                for switch_ip, _ in nexus_switches))
        else:
            raise cexc.NoNexusSviSwitch()

        try:
            bindings = nxos_db.get_nexussvi_bindings()

            # Build a switch dictionary with weights
            for binding in bindings:
                switch_ip = binding.switch_ip
                if switch_ip not in switch_dict:
                    switch_dict[switch_ip] = 1
                else:
                    switch_dict[switch_ip] += 1

            # Search for the lowest value in the dict
            if switch_dict:
                switch_ip = min(switch_dict, key=switch_dict.get)

        except cexc.NexusPortBindingNotFound:
            # First SVI binding, assign any switch IP configured.
            switch_ip = switch_dict.keys()[0]

        LOG.debug("SVI switch used: %s" % switch_ip)
        return switch_ip

    def _add_nexus_svi_db(self, switch_ip, router_id, vlan_id, subnet_id):
        """Create SVI database nexus switch entry."""
        try:
            nxos_db.get_nexusvm_bindings(vlan_id, router_id)
            raise cexc.SubnetInterfacePresent(subnet_id=subnet_id,
                                              router_id=router_id)
        except cexc.NexusPortBindingNotFound:
            nxos_db.add_nexusport_binding('router', str(vlan_id), 0,
                                          switch_ip, router_id, False)

    def _add_nexus_svi_interface(self, switch_ip, router_id, vlan_id, subnet):
        """Create SVI nexus switch entries."""
        gateway_ip = subnet['gateway_ip']
        cidr = subnet['cidr']
        netmask = cidr.split('/', 1)[1]
        gateway_ip = gateway_ip + '/' + netmask
        vlan_name = cfg.CONF.ml2_cisco.vlan_name_prefix + str(vlan_id)

        # Create vlan interface on switch if it doesn't already exist.
        bindings = nxos_db.get_nexusvlan_binding(vlan_id, switch_ip)
        if len(bindings) == 1:
            self.driver.create_vlan(switch_ip, vlan_id, vlan_name, 0)

        # Create SVI interface entry.
        bindings = nxos_db.get_nexusvm_bindings(vlan_id, router_id)
        if len(bindings) == 1:
            self.driver.create_vlan_svi(switch_ip, vlan_id, gateway_ip)

    def _remove_nexus_svi_db(self, switch_ip, router_id, vlan_id):
        """Delete SVI database nexus switch entries."""
        nxos_db.remove_nexusport_binding('router', str(vlan_id), 0,
                                         switch_ip, router_id, False)

    def _remove_nexus_svi_interface(self, switch_ip, vlan_id):
        """Delete SVI nexus switch entries."""

        # Delete the SVI interface from the nexus switch.
        self.driver.delete_vlan_svi(switch_ip, vlan_id)

        # if there are no remaining db entries using this vlan on this
        # nexus switch then remove the vlan.
        try:
            nxos_db.get_nexusvlan_binding(vlan_id, switch_ip)
        except cexc.NexusPortBindingNotFound:
            self.driver.delete_vlan(switch_ip, vlan_id)

    def _add_router_db(self, context, router_id, interface_info, switch_ip,
                       vlan_id, subnet_id):
        """Create all database entries."""
        new_intf = (super(CiscoNexusL3ServicePlugin, self).
                    add_router_interface(context, router_id, interface_info))
        self._add_nexus_svi_db(switch_ip, router_id, vlan_id, subnet_id)
        return new_intf

    def _remove_router_db(self, context, router_id, interface_info, switch_ip,
                          vlan_id):
        """Delete all database entries."""
        new_intf = (super(CiscoNexusL3ServicePlugin, self).
                remove_router_interface(context, router_id, interface_info))
        self._remove_nexus_svi_db(switch_ip, router_id, vlan_id)
        return new_intf

    def add_router_interface(self, context, router_id, interface_info):
        """Create SVI interface on a Nexus switch."""
        if 'subnet_id' not in interface_info:
            raise cexc.SubnetNotSpecified()
        if 'port_id' in interface_info:
            raise cexc.PortIdForNexusSvi()

        subnet_id = interface_info['subnet_id']
        subnet = self.get_subnet(context, subnet_id)
        vlan_id = self._get_vlanid(context, subnet)

        LOG.debug("Attaching subnet %(subnet_id)s to "
                  "router %(router_id)s" % {'subnet_id': subnet_id,
                                            'router_id': router_id})

        # Find a switch to create the SVI on.
        switch_ip = self._find_switch_for_svi()

        # Create the entry in the databases.
        with context.session.begin(subtransactions=True):
            new_intf = self._add_router_db(context, router_id, interface_info,
                                           switch_ip, vlan_id, subnet_id)

        # Create the entry on the nexus switch.
        try:
            self._add_nexus_svi_interface(switch_ip, router_id, vlan_id,
                                          subnet)
        except Exception:
            LOG.error(_("Error attaching subnet %(subnet_id)s to "
                        "router %(router_id)s") % {'subnet_id': subnet_id,
                                                   'router_id': router_id})
            with excutils.save_and_reraise_exception():
                self._remove_router_db(context, router_id, interface_info,
                                       switch_ip, vlan_id)
        return new_intf

    def remove_router_interface(self, context, router_id, interface_info):
        """Delete SVI interface on a Nexus switch."""
        subnet_id = interface_info['subnet_id']
        subnet = self.get_subnet(context, subnet_id)
        vlan_id = self._get_vlanid(context, subnet)

        LOG.debug("Detaching subnet %(subnet_id)s from "
                  "router %(router_id)s" % {'subnet_id': subnet_id,
                                            'router_id': router_id})

        # Find switch_ip from database.
        switch_ip = nxos_db.get_nexusvm_bindings(vlan_id,
                                                 router_id)[0].switch_ip

        # Delete the entry from the databases.
        with context.session.begin(subtransactions=True):
            new_intf = self._remove_router_db(context, router_id,
                                        interface_info, switch_ip, vlan_id)

        # Delete the entry from the nexus switch.
        try:
            self._remove_nexus_svi_interface(switch_ip, vlan_id)
        except Exception:
            LOG.error(_("Error detaching subnet %(subnet_id)s from "
                        "router %(router_id)s") % {'subnet_id': subnet_id,
                                                   'router_id': router_id})
            with excutils.save_and_reraise_exception():
                self._add_router_db(context, router_id, interface_info,
                                    switch_ip, vlan_id, subnet_id)
        return new_intf
