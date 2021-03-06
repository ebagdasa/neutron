# Copyright 2012, Nachi Ueno, NTT MCL, Inc.
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

import netaddr
from oslo.config import cfg

from neutron.agent import firewall
from neutron.agent.linux import ebtables_manager
from neutron.agent.linux import ipset_manager
from neutron.agent.linux import iptables_comments as ic
from neutron.agent.linux import iptables_manager
from neutron.common import constants
from neutron.common import ipv6_utils
from neutron.i18n import _LI
from neutron.openstack.common import log as logging


LOG = logging.getLogger(__name__)
SG_CHAIN = 'sg-chain'
INGRESS_DIRECTION = 'ingress'
EGRESS_DIRECTION = 'egress'
SPOOF_FILTER = 'spoof-filter'
CHAIN_NAME_PREFIX = {INGRESS_DIRECTION: 'i',
                     EGRESS_DIRECTION: 'o',
                     SPOOF_FILTER: 's'}
SPOOFING_CHAIN_NAME_PREFIX_ARP = '-arp-'
SPOOFING_CHAIN_NAME_PREFIX_IP = '-ip-'
DIRECTION_IP_PREFIX = {'ingress': 'source_ip_prefix',
                       'egress': 'dest_ip_prefix'}
IPSET_DIRECTION = {INGRESS_DIRECTION: 'src',
                   EGRESS_DIRECTION: 'dst'}
LINUX_DEV_LEN = 14
IPSET_CHAIN_LEN = 20
IPSET_CHANGE_BULK_THRESHOLD = 10
IPSET_ADD_BULK_THRESHOLD = 5
comment_rule = iptables_manager.comment_rule


class NWFilterFirewall(object):
    """
    This class implements a network filtering mechanism by using
    ebtables.

    All ports get a base filter applied. This filter provides some basic
    security such as protection against MAC spoofing, IP spoofing, and ARP
    spoofing.
    """

    def __init__(self):
        self.ebtables = ebtables_manager.EbtablesManager(
            root_helper=cfg.CONF.AGENT.root_helper,
            prefix_chain='neutron-nwfilter')
        with ebtables_manager.EbtablesManagerTransaction(self.ebtables):
            del self.ebtables.tables['nat']
            del self.ebtables.tables['broute']
            table = self.ebtables.tables['filter']
            self.fallback_chain_name = self._add_fallback_chain(table)

    def setup_basic_filtering(self, port_id, device, mac_ip_pairs):
        if port_id is None or device is None:
            return
        if mac_ip_pairs is None or mac_ip_pairs[0] is None:
            return

        table = self.ebtables.tables['filter']

        # Set rules and chains for the device
        self._setup_device_chains(table, port_id, device)

        # Set anti ARP spoofing
        arp_rules = self._setup_arp_antispoofing(mac_ip_pairs)
        arp_rules += ['-j $%s' % self.fallback_chain_name]
        arp_chain_name = self._port_chain_name(SPOOFING_CHAIN_NAME_PREFIX_ARP
                                               + port_id, INGRESS_DIRECTION)
        self._set_chain_and_rules(table, arp_chain_name, arp_rules)
        rules = ['-p arp -j $%s' % arp_chain_name]
        self._set_rules_for_device(table, port_id, rules,
                                   INGRESS_DIRECTION)

        # Set the MAC/IP anti spoofing rules and allow DHCP traffic
        # NOTE(ethuleau): more?
        # - IPv6 neighbor discovery reflection filter
        # - IPv6 RA advertisement filter
        # - ...
        # Should we implement _accept_inbound_icmpv6 (line 374) method here?
        ip_rules = self._allow_dhcp_request()
        ip_rules += self._drop_dhcp_offer_rule()
        ip_rules += self._setup_mac_ip_antispoofing(mac_ip_pairs)
        ip_rules += ['-j $%s' % self.fallback_chain_name]
        ip_chain_name = self._port_chain_name(SPOOFING_CHAIN_NAME_PREFIX_IP
                                              + port_id, INGRESS_DIRECTION)
        self._set_chain_and_rules(table, ip_chain_name, ip_rules)
        jump_rules = ['-p IPv4 -j $%s' % ip_chain_name]
        jump_rules += ['-p IPv6 -j $%s' % ip_chain_name]
        self._set_rules_for_device(table, port_id, jump_rules,
                                   INGRESS_DIRECTION)

        self.ebtables.apply()

    def unfilter_instance(self, port_id):
        if port_id is None:
            return

        table = self.ebtables.tables['filter']

        chain_name = self._port_chain_name(port_id, INGRESS_DIRECTION)
        table.ensure_remove_chain(chain_name)

        arp_chain_name = self._port_chain_name(SPOOFING_CHAIN_NAME_PREFIX_ARP +
                                               port_id,
                                               INGRESS_DIRECTION)
        table.ensure_remove_chain(arp_chain_name)

        ip_chain_name = self._port_chain_name(SPOOFING_CHAIN_NAME_PREFIX_IP +
                                              port_id,
                                              INGRESS_DIRECTION)
        table.ensure_remove_chain(ip_chain_name)

        self.ebtables.apply()

    def defer_apply_on(self):
        self.ebtables.defer_apply_on()

    def defer_apply_off(self):
        self.ebtables.defer_apply_off()

    def _setup_device_chains(self, table, port_id, device):
        # Add chain and jump to all incoming from the device
        chain_name = self._port_chain_name(port_id, INGRESS_DIRECTION)
        table.add_chain(chain_name)
        rule = '--in-interface %s -j $%s' % (device, chain_name)
        table.add_rule('FORWARD', rule, top=True)

        # NOTE(ethuleau): Do we need to apply some filters on traffic going to
        # the device (eg. DHCP request of neighbors)?

    def _allow_dhcp_request(self):
        # Note(ethuleau): The sg mixin already set default provider sg to
        # protect DHCP traffic (neutron/db/securitygroups_rpc_base.py, line
        # 254).
        # Only incoming DHCP server traffic is authorize from DHCP
        # servers IP and DHCP server traffic is drop if it's coming from the
        # VM. Should we support that in the NWFilterFirewall class?
        rules = []
        for proto in ['udp', 'tcp']:
            # NOTE(ethuleau): we limit DHCP request to not overload DHCP agents
            # One request per second with a burst of 5 (default value) is
            # enough?
            rules += ['-p IPv4 --ip-proto %s --ip-sport 68 --ip-dport 67 '
                      '--limit 1/s -j ACCEPT' % proto]
        return rules

    def _drop_dhcp_offer_rule(self):
        rules = []
        for proto in ['udp', 'tcp']:
            rules += ['-p IPv4 --ip-proto %s --ip-sport 67 --ip-dport 68 '
                      '-j DROP' % proto]
        return rules

    def _setup_mac_ip_antispoofing(self, mac_ip_pairs):
        rules = []
        for mac, ip in mac_ip_pairs:
            if ip is None:
                rules += ['-p %s -j RETURN' % mac]
            else:
                rules += ['-s %s -p %s --ip-source %s -j RETURN' %
                          (mac, self._get_ip_protocol(ip), ip)]
        return rules

    def _setup_arp_antispoofing(self, mac_ip_pairs):
        rules = []
        for mac, ip in mac_ip_pairs:
            if ip is not None:
                rules += [('-p arp --arp-opcode 2 --arp-mac-src %s '
                           '--arp-ip-src %s -j RETURN') % (mac, ip)]
        rules += ['-p ARP --arp-op Request -j ACCEPT']
        return rules

    def _add_fallback_chain(self, table):
        table.add_chain('spoofing-fallback')
        table.add_rule('spoofing-fallback', '-j DROP')
        return self.ebtables.get_chain_name('spoofing-fallback')

    def _port_chain_name(self, port_id, direction):
        return self.ebtables.get_chain_name(
            '%s%s' % (CHAIN_NAME_PREFIX[direction], port_id))

    def _get_ip_protocol(self, ip_address):
        if netaddr.IPNetwork(ip_address).version == 4:
            return 'IPv4'
        else:
            return 'IPv6'

    def _set_rules_for_device(self, table, port_id, rules, direction):
        chain_name = self._port_chain_name(port_id, direction)
        for rule in rules:
            table.add_rule(chain_name, rule)

    def _set_chain_and_rules(self, table, chain_name, rules):
        table.add_chain(chain_name)
        for rule in rules:
            table.add_rule(chain_name, rule)


class IptablesFirewallDriver(firewall.FirewallDriver):
    """Driver which enforces security groups through iptables rules."""
    IPTABLES_DIRECTION = {INGRESS_DIRECTION: 'physdev-out',
                          EGRESS_DIRECTION: 'physdev-in'}

    def __init__(self):
        self.root_helper = cfg.CONF.AGENT.root_helper
        self.iptables = iptables_manager.IptablesManager(
            root_helper=self.root_helper,
            use_ipv6=ipv6_utils.is_enabled())
        # TODO(majopela, shihanzhang): refactor out ipset to a separate
        # driver composed over this one
        self.ipset = ipset_manager.IpsetManager(root_helper=self.root_helper)
        # list of port which has security group
        self.filtered_ports = {}
        self._add_fallback_chain_v4v6()
        self._defer_apply = False
        self._pre_defer_filtered_ports = None
        # List of security group rules for ports residing on this host
        self.sg_rules = {}
        self.pre_sg_rules = None
        # List of security group member ips for ports residing on this host
        self.sg_members = {}
        self.pre_sg_members = None
        self.ipset_chains = {}
        self.enable_ipset = cfg.CONF.SECURITYGROUP.enable_ipset
        self.nwfilter = NWFilterFirewall()

    @property
    def ports(self):
        return self.filtered_ports

    def update_security_group_rules(self, sg_id, sg_rules):
        LOG.debug("Update rules of security group (%s)", sg_id)
        self.sg_rules[sg_id] = sg_rules

    def update_security_group_members(self, sg_id, sg_members):
        LOG.debug("Update members of security group (%s)", sg_id)
        self.sg_members[sg_id] = sg_members

    def prepare_port_filter(self, port):
        LOG.debug("Preparing device (%s) filter", port['device'])
        self._remove_chains()
        self.filtered_ports[port['device']] = port
        # each security group has it own chains
        self._setup_chains()
        self.iptables.apply()

    def update_port_filter(self, port):
        LOG.debug("Updating device (%s) filter", port['device'])
        if port['device'] not in self.filtered_ports:
            LOG.info(_LI('Attempted to update port filter which is not '
                         'filtered %s'), port['device'])
            return
        self._remove_chains()
        self.filtered_ports[port['device']] = port
        self._setup_chains()
        self.iptables.apply()

    def remove_port_filter(self, port):
        LOG.debug("Removing device (%s) filter", port['device'])
        if not self.filtered_ports.get(port['device']):
            LOG.info(_LI('Attempted to remove port filter which is not '
                         'filtered %r'), port)
            return
        self._remove_chains()
        self.filtered_ports.pop(port['device'], None)
        self._setup_chains()
        self.iptables.apply()

    def _setup_chains(self):
        """Setup ingress and egress chain for a port."""
        if not self._defer_apply:
            self._setup_chains_apply(self.filtered_ports)

    def _setup_chains_apply(self, ports):
        self._add_chain_by_name_v4v6(SG_CHAIN)
        for port in ports.values():
            self._setup_chain(port, INGRESS_DIRECTION)
            self._setup_chain(port, EGRESS_DIRECTION)
            self.iptables.ipv4['filter'].add_rule(SG_CHAIN, '-j ACCEPT')
            self.iptables.ipv6['filter'].add_rule(SG_CHAIN, '-j ACCEPT')

    def _remove_chains(self):
        """Remove ingress and egress chain for a port."""
        if not self._defer_apply:
            self._remove_chains_apply(self.filtered_ports)

    def _remove_chains_apply(self, ports):
        for port in ports.values():
            self._remove_chain(port, INGRESS_DIRECTION)
            self._remove_chain(port, EGRESS_DIRECTION)
            self._remove_chain(port, SPOOF_FILTER)
            self.nwfilter.unfilter_instance(port['id'])
        self._remove_chain_by_name_v4v6(SG_CHAIN)

    def _setup_chain(self, port, DIRECTION):
        self._add_chain(port, DIRECTION)
        self._add_rule_by_security_group(port, DIRECTION)

    def _remove_chain(self, port, DIRECTION):
        chain_name = self._port_chain_name(port, DIRECTION)
        self._remove_chain_by_name_v4v6(chain_name)

    def _add_fallback_chain_v4v6(self):
        self.iptables.ipv4['filter'].add_chain('sg-fallback')
        self.iptables.ipv4['filter'].add_rule('sg-fallback', '-j DROP',
                                              comment=ic.UNMATCH_DROP)
        self.iptables.ipv6['filter'].add_chain('sg-fallback')
        self.iptables.ipv6['filter'].add_rule('sg-fallback', '-j DROP',
                                              comment=ic.UNMATCH_DROP)

    def _add_chain_by_name_v4v6(self, chain_name):
        self.iptables.ipv6['filter'].add_chain(chain_name)
        self.iptables.ipv4['filter'].add_chain(chain_name)

    def _remove_chain_by_name_v4v6(self, chain_name):
        self.iptables.ipv4['filter'].remove_chain(chain_name)
        self.iptables.ipv6['filter'].remove_chain(chain_name)

    def _add_rule_to_chain_v4v6(self, chain_name, ipv4_rules, ipv6_rules,
                                comment=None):
        for rule in ipv4_rules:
            self.iptables.ipv4['filter'].add_rule(chain_name, rule,
                                                  comment=comment)

        for rule in ipv6_rules:
            self.iptables.ipv6['filter'].add_rule(chain_name, rule,
                                                  comment=comment)

    def _get_device_name(self, port):
        return port['device']

    def _add_chain(self, port, direction):
        chain_name = self._port_chain_name(port, direction)
        self._add_chain_by_name_v4v6(chain_name)

        # Note(nati) jump to the security group chain (SG_CHAIN)
        # This is needed because the packet may much two rule in port
        # if the two port is in the same host
        # We accept the packet at the end of SG_CHAIN.

        # jump to the security group chain
        device = self._get_device_name(port)
        jump_rule = ['-m physdev --%s %s --physdev-is-bridged '
                     '-j $%s' % (self.IPTABLES_DIRECTION[direction],
                                 device,
                                 SG_CHAIN)]
        self._add_rule_to_chain_v4v6('FORWARD', jump_rule, jump_rule,
                                     comment=ic.VM_INT_SG)

        # jump to the chain based on the device
        jump_rule = ['-m physdev --%s %s --physdev-is-bridged '
                     '-j $%s' % (self.IPTABLES_DIRECTION[direction],
                                 device,
                                 chain_name)]
        self._add_rule_to_chain_v4v6(SG_CHAIN, jump_rule, jump_rule,
                                     comment=ic.SG_TO_VM_SG)

        if direction == EGRESS_DIRECTION:
            self._add_rule_to_chain_v4v6('INPUT', jump_rule, jump_rule,
                                         comment=ic.INPUT_TO_SG)

    def _split_sgr_by_ethertype(self, security_group_rules):
        ipv4_sg_rules = []
        ipv6_sg_rules = []
        for rule in security_group_rules:
            if rule.get('ethertype') == constants.IPv4:
                ipv4_sg_rules.append(rule)
            elif rule.get('ethertype') == constants.IPv6:
                if rule.get('protocol') == 'icmp':
                    rule['protocol'] = 'icmpv6'
                ipv6_sg_rules.append(rule)
        return ipv4_sg_rules, ipv6_sg_rules

    def _select_sgr_by_direction(self, port, direction):
        return [rule
                for rule in port.get('security_group_rules', [])
                if rule['direction'] == direction]

    def _get_mac_ip_pair(self, port):
        # Note(ethuleau): Why? Insted, we should drop RA from a VM port.
        #ipv6_rules += ['-p icmpv6 -j RETURN']
        mac_ip_pairs = []

        if isinstance(port.get('allowed_address_pairs'), list):
            for address_pair in port['allowed_address_pairs']:
                mac_ip_pairs.append((address_pair['mac_address'],
                                     address_pair['ip_address']))

        for ip in port['fixed_ips']:
            mac_ip_pairs.append((port['mac_address'], ip))

        if not port['fixed_ips']:
            mac_ip_pairs.append((port['mac_address'], None))

        return mac_ip_pairs

    def _setup_spoof_filter_chain(self, port, table, mac_ip_pairs, rules):
        if mac_ip_pairs:
            chain_name = self._port_chain_name(port, SPOOF_FILTER)
            table.add_chain(chain_name)
            for mac, ip in mac_ip_pairs:
                if ip is None:
                    # If fixed_ips is [] this rule will be added to the end
                    # of the list after the allowed_address_pair rules.
                    table.add_rule(chain_name,
                                   '-m mac --mac-source %s -j RETURN'
                                   % mac, comment=ic.PAIR_ALLOW)
                else:
                    table.add_rule(chain_name,
                                   '-m mac --mac-source %s -s %s -j RETURN'
                                   % (mac, ip), comment=ic.PAIR_ALLOW)
            table.add_rule(chain_name, '-j DROP', comment=ic.PAIR_DROP)
            rules.append('-j $%s' % chain_name)

    def _build_ipv4v6_mac_ip_list(self, mac, ip_address, mac_ipv4_pairs,
                                  mac_ipv6_pairs):
        if netaddr.IPNetwork(ip_address).version == 4:
            mac_ipv4_pairs.append((mac, ip_address))
        else:
            mac_ipv6_pairs.append((mac, ip_address))

    def _spoofing_rule(self, port, ipv4_rules, ipv6_rules):
        #Note(nati) allow dhcp or RA packet
        ipv4_rules += [comment_rule('-p udp -m udp --sport 68 --dport 67 '
                                    '-j RETURN', comment=ic.DHCP_CLIENT)]
        ipv6_rules += [comment_rule('-p icmpv6 -j RETURN',
                                    comment=ic.IPV6_RA_ALLOW)]
        ipv6_rules += [comment_rule('-p udp -m udp --sport 546 --dport 547 '
                                    '-j RETURN', comment=None)]
        mac_ipv4_pairs = []
        mac_ipv6_pairs = []

        if isinstance(port.get('allowed_address_pairs'), list):
            for address_pair in port['allowed_address_pairs']:
                self._build_ipv4v6_mac_ip_list(address_pair['mac_address'],
                                               address_pair['ip_address'],
                                               mac_ipv4_pairs,
                                               mac_ipv6_pairs)

        for ip in port['fixed_ips']:
            self._build_ipv4v6_mac_ip_list(port['mac_address'], ip,
                                           mac_ipv4_pairs, mac_ipv6_pairs)
        if not port['fixed_ips']:
            mac_ipv4_pairs.append((port['mac_address'], None))
            mac_ipv6_pairs.append((port['mac_address'], None))

        self._setup_spoof_filter_chain(port, self.iptables.ipv4['filter'],
                                       mac_ipv4_pairs, ipv4_rules)
        self._setup_spoof_filter_chain(port, self.iptables.ipv6['filter'],
                                       mac_ipv6_pairs, ipv6_rules)

    def _drop_dhcp_rule(self, ipv4_rules, ipv6_rules):
        #Note(nati) Drop dhcp packet from VM
        ipv4_rules += [comment_rule('-p udp -m udp --sport 67 --dport 68 '
                                    '-j DROP', comment=ic.DHCP_SPOOF)]
        ipv6_rules += [comment_rule('-p udp -m udp --sport 547 --dport 546 '
                                    '-j DROP', comment=None)]

    def _accept_inbound_icmpv6(self):
        # Allow multicast listener, neighbor solicitation and
        # neighbor advertisement into the instance
        icmpv6_rules = []
        for icmp6_type in constants.ICMPV6_ALLOWED_TYPES:
            icmpv6_rules += ['-p icmpv6 --icmpv6-type %s -j RETURN' %
                             icmp6_type]
        return icmpv6_rules

    def _select_sg_rules_for_port(self, port, direction):
        sg_ids = port.get('security_groups', [])
        port_rules = []
        fixed_ips = port.get('fixed_ips', [])
        for sg_id in sg_ids:
            for rule in self.sg_rules.get(sg_id, []):
                if rule['direction'] == direction:
                    if self.enable_ipset:
                        port_rules.append(rule)
                        continue
                    remote_group_id = rule.get('remote_group_id')
                    if not remote_group_id:
                        port_rules.append(rule)
                        continue
                    ethertype = rule['ethertype']
                    for ip in self.sg_members[remote_group_id][ethertype]:
                        if ip in fixed_ips:
                            continue
                        ip_rule = rule.copy()
                        direction_ip_prefix = DIRECTION_IP_PREFIX[direction]
                        ip_rule[direction_ip_prefix] = str(
                            netaddr.IPNetwork(ip).cidr)
                        port_rules.append(ip_rule)
        return port_rules

    def _get_remote_sg_ids(self, port, direction):
        sg_ids = port.get('security_groups', [])
        remote_sg_ids = {constants.IPv4: [], constants.IPv6: []}
        for sg_id in sg_ids:
            for rule in self.sg_rules.get(sg_id, []):
                if rule['direction'] == direction:
                    remote_sg_id = rule.get('remote_group_id')
                    ether_type = rule.get('ethertype')
                    if remote_sg_id and ether_type:
                        remote_sg_ids[ether_type].append(remote_sg_id)
        return remote_sg_ids

    def _add_rule_by_security_group(self, port, direction):
        chain_name = self._port_chain_name(port, direction)
        # select rules for current direction
        security_group_rules = self._select_sgr_by_direction(port, direction)
        security_group_rules += self._select_sg_rules_for_port(port, direction)
        if self.enable_ipset:
            remote_sg_ids = self._get_remote_sg_ids(port, direction)
            # update the corresponding ipset chain member
            self._update_ipset_chain_member(remote_sg_ids)
        # split groups by ip version
        # for ipv4, iptables command is used
        # for ipv6, iptables6 command is used
        ipv4_sg_rules, ipv6_sg_rules = self._split_sgr_by_ethertype(
            security_group_rules)
        ipv4_iptables_rule = []
        ipv6_iptables_rule = []
        if direction == EGRESS_DIRECTION:
            self.nwfilter.setup_basic_filtering(port['id'],
                                                self._get_device_name(port),
                                                self._get_mac_ip_pair(port))
            self._spoofing_rule(port,
                                ipv4_iptables_rule,
                                ipv6_iptables_rule)
            self._drop_dhcp_rule(ipv4_iptables_rule, ipv6_iptables_rule)
        if direction == INGRESS_DIRECTION:
            ipv6_iptables_rule += self._accept_inbound_icmpv6()
        ipv4_iptables_rule += self._convert_sgr_to_iptables_rules(
            ipv4_sg_rules)
        ipv6_iptables_rule += self._convert_sgr_to_iptables_rules(
            ipv6_sg_rules)
        self._add_rule_to_chain_v4v6(chain_name,
                                     ipv4_iptables_rule,
                                     ipv6_iptables_rule)

    def _get_cur_sg_member_ips(self, sg_id, ethertype):
        return self.sg_members.get(sg_id, {}).get(ethertype, [])

    def _get_pre_sg_member_ips(self, sg_id, ethertype):
        return self.pre_sg_members.get(sg_id, {}).get(ethertype, [])

    def _get_new_sg_member_ips(self, sg_id, ethertype):
        add_member_ips = (set(self._get_cur_sg_member_ips(sg_id, ethertype)) -
                          set(self._get_pre_sg_member_ips(sg_id, ethertype)))
        return list(add_member_ips)

    def _get_deleted_sg_member_ips(self, sg_id, ethertype):
        del_member_ips = (set(self._get_pre_sg_member_ips(sg_id, ethertype)) -
                          set(self._get_cur_sg_member_ips(sg_id, ethertype)))
        return list(del_member_ips)

    def _bulk_set_ips_to_chain(self, chain_name, member_ips, ethertype):
        self.ipset.refresh_ipset_chain_by_name(chain_name, member_ips,
                                               ethertype)
        self.ipset_chains[chain_name] = member_ips

    def _add_ips_to_ipset_chain(self, chain_name, add_ips):
        for ip in add_ips:
            if ip not in self.ipset_chains[chain_name]:
                self.ipset.add_member_to_ipset_chain(chain_name, ip)
                self.ipset_chains[chain_name].append(ip)

    def _del_ips_from_ipset_chain(self, chain_name, del_ips):
        if chain_name in self.ipset_chains:
            for del_ip in del_ips:
                if del_ip in self.ipset_chains[chain_name]:
                    self.ipset.del_ipset_chain_member(chain_name, del_ip)
                    self.ipset_chains[chain_name].remove(del_ip)

    def _update_ipset_chain_member(self, security_group_ids):
        for ethertype, sg_ids in security_group_ids.items():
            for sg_id in sg_ids:
                add_ips = self._get_new_sg_member_ips(sg_id, ethertype)
                del_ips = self._get_deleted_sg_member_ips(sg_id, ethertype)
                cur_member_ips = self._get_cur_sg_member_ips(sg_id, ethertype)
                chain_name = ethertype + sg_id[:IPSET_CHAIN_LEN]
                if chain_name not in self.ipset_chains and cur_member_ips:
                    self.ipset_chains[chain_name] = []
                    self.ipset.create_ipset_chain(chain_name, ethertype)
                    self._bulk_set_ips_to_chain(chain_name,
                                                cur_member_ips, ethertype)
                elif (len(add_ips) + len(del_ips)
                      < IPSET_CHANGE_BULK_THRESHOLD):
                    self._add_ips_to_ipset_chain(chain_name, add_ips)
                    self._del_ips_from_ipset_chain(chain_name, del_ips)
                else:
                    self._bulk_set_ips_to_chain(chain_name,
                                                cur_member_ips, ethertype)

    def _generate_ipset_chain(self, sg_rule, remote_gid):
        iptables_rules = []
        args = self._protocol_arg(sg_rule.get('protocol'))
        args += self._port_arg('sport',
                               sg_rule.get('protocol'),
                               sg_rule.get('source_port_range_min'),
                               sg_rule.get('source_port_range_max'))
        args += self._port_arg('dport',
                               sg_rule.get('protocol'),
                               sg_rule.get('port_range_min'),
                               sg_rule.get('port_range_max'))
        direction = sg_rule.get('direction')
        ethertype = sg_rule.get('ethertype')
        # the length of ipset chain name require less than 31
        # characters
        ipset_chain_name = (ethertype + remote_gid[:IPSET_CHAIN_LEN])
        if ipset_chain_name in self.ipset_chains:
            args += ['-m set', '--match-set',
                     ipset_chain_name,
                     IPSET_DIRECTION[direction]]
            args += ['-j RETURN']
            iptables_rules += [' '.join(args)]
        return iptables_rules

    def _convert_sgr_to_iptables_rules(self, security_group_rules):
        iptables_rules = []
        self._drop_invalid_packets(iptables_rules)
        self._allow_established(iptables_rules)
        for rule in security_group_rules:
            if self.enable_ipset:
                remote_gid = rule.get('remote_group_id')
                if remote_gid:
                    iptables_rules.extend(
                        self._generate_ipset_chain(rule, remote_gid))
                    continue
            # These arguments MUST be in the format iptables-save will
            # display them: source/dest, protocol, sport, dport, target
            # Otherwise the iptables_manager code won't be able to find
            # them to preserve their [packet:byte] counts.
            args = self._ip_prefix_arg('s',
                                       rule.get('source_ip_prefix'))
            args += self._ip_prefix_arg('d',
                                        rule.get('dest_ip_prefix'))
            args += self._protocol_arg(rule.get('protocol'))
            args += self._port_arg('sport',
                                   rule.get('protocol'),
                                   rule.get('source_port_range_min'),
                                   rule.get('source_port_range_max'))
            args += self._port_arg('dport',
                                   rule.get('protocol'),
                                   rule.get('port_range_min'),
                                   rule.get('port_range_max'))
            args += ['-j RETURN']
            iptables_rules += [' '.join(args)]

        iptables_rules += [comment_rule('-j $sg-fallback',
                                        comment=ic.UNMATCHED)]

        return iptables_rules

    def _drop_invalid_packets(self, iptables_rules):
        # Always drop invalid packets
        iptables_rules += [comment_rule('-m state --state ' 'INVALID -j DROP',
                                        comment=ic.STATELESS_DROP)]
        return iptables_rules

    def _allow_established(self, iptables_rules):
        # Allow established connections
        iptables_rules += [comment_rule(
            '-m state --state RELATED,ESTABLISHED -j RETURN',
            comment=ic.ALLOW_ASSOC)]
        return iptables_rules

    def _protocol_arg(self, protocol):
        if not protocol:
            return []

        iptables_rule = ['-p', protocol]
        # iptables always adds '-m protocol' for udp and tcp
        if protocol in ['udp', 'tcp']:
            iptables_rule += ['-m', protocol]
        return iptables_rule

    def _port_arg(self, direction, protocol, port_range_min, port_range_max):
        if (protocol not in ['udp', 'tcp', 'icmp', 'icmpv6']
            or not port_range_min):
            return []

        if protocol in ['icmp', 'icmpv6']:
            # Note(xuhanp): port_range_min/port_range_max represent
            # icmp type/code when protocol is icmp or icmpv6
            # icmp code can be 0 so we cannot use "if port_range_max" here
            if port_range_max is not None:
                return ['--%s-type' % protocol,
                        '%s/%s' % (port_range_min, port_range_max)]
            return ['--%s-type' % protocol, '%s' % port_range_min]
        elif port_range_min == port_range_max:
            return ['--%s' % direction, '%s' % (port_range_min,)]
        else:
            return ['-m', 'multiport',
                    '--%ss' % direction,
                    '%s:%s' % (port_range_min, port_range_max)]

    def _ip_prefix_arg(self, direction, ip_prefix):
        #NOTE (nati) : source_group_id is converted to list of source_
        # ip_prefix in server side
        if ip_prefix:
            return ['-%s' % direction, ip_prefix]
        return []

    def _port_chain_name(self, port, direction):
        return iptables_manager.get_chain_name(
            '%s%s' % (CHAIN_NAME_PREFIX[direction], port['device'][3:]))

    def filter_defer_apply_on(self):
        if not self._defer_apply:
            self.iptables.defer_apply_on()
            self.nwfilter.defer_apply_on()
            self._pre_defer_filtered_ports = dict(self.filtered_ports)
            self.pre_sg_members = dict(self.sg_members)
            self.pre_sg_rules = dict(self.sg_rules)
            self._defer_apply = True

    def _remove_unused_security_group_info(self):
        need_removed_ipset_chains = {constants.IPv4: set(),
                                     constants.IPv6: set()}
        need_removed_security_groups = set()
        remote_group_ids = {constants.IPv4: set(),
                            constants.IPv6: set()}
        cur_group_ids = set()
        for port in self.filtered_ports.values():
            for direction in INGRESS_DIRECTION, EGRESS_DIRECTION:
                for ethertype, sg_ids in self._get_remote_sg_ids(
                        port, direction).items():
                    remote_group_ids[ethertype].update(sg_ids)
            groups = port.get('security_groups', [])
            cur_group_ids.update(groups)

        for ethertype in [constants.IPv4, constants.IPv6]:
            need_removed_ipset_chains[ethertype].update(
                [x for x in self.pre_sg_members if x not in remote_group_ids[
                    ethertype]])
            need_removed_security_groups.update(
                [x for x in self.pre_sg_rules if x not in cur_group_ids])

        # Remove unused remote ipset set
        for ethertype, remove_chain_ids in need_removed_ipset_chains.items():
            for remove_chain_id in remove_chain_ids:
                if self.sg_members.get(remove_chain_id, {}).get(ethertype, []):
                    self.sg_members[remove_chain_id][ethertype] = []
                if self.enable_ipset:
                    removed_chain = (
                        ethertype + remove_chain_id[:IPSET_CHAIN_LEN])
                    if removed_chain in self.ipset_chains:
                        self.ipset.destroy_ipset_chain_by_name(
                            removed_chain)
                        self.ipset_chains.pop(removed_chain, None)

        # Remove unused remote security group member ips
        sg_ids = self.sg_members.keys()
        for sg_id in sg_ids:
            if not (self.sg_members[sg_id].get(constants.IPv4, [])
                    or self.sg_members[sg_id].get(constants.IPv6, [])):
                self.sg_members.pop(sg_id, None)

        # Remove unused security group rules
        for remove_group_id in need_removed_security_groups:
            if remove_group_id in self.sg_rules:
                self.sg_rules.pop(remove_group_id, None)

    def filter_defer_apply_off(self):
        if self._defer_apply:
            self._defer_apply = False
            self._remove_chains_apply(self._pre_defer_filtered_ports)
            self._setup_chains_apply(self.filtered_ports)
            self.iptables.defer_apply_off()
            self._remove_unused_security_group_info()
            self._pre_defer_filtered_ports = None
            self.nwfilter.defer_apply_off()


class OVSHybridIptablesFirewallDriver(IptablesFirewallDriver):
    OVS_HYBRID_TAP_PREFIX = constants.TAP_DEVICE_PREFIX

    def _port_chain_name(self, port, direction):
        return iptables_manager.get_chain_name(
            '%s%s' % (CHAIN_NAME_PREFIX[direction], port['device']))

    def _get_device_name(self, port):
        return (self.OVS_HYBRID_TAP_PREFIX + port['device'])[:LINUX_DEV_LEN]
