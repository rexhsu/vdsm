#!/usr/bin/env python
# Copyright 2016 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#
from __future__ import print_function

from contextlib import contextmanager
import sys
import traceback
import xml.etree.cElementTree as ET

from vdsm import jsonrpcvdscli
from vdsm.config import config
from vdsm.network import api as net_api


_DEBUG_MODE = False
LOG_FILE = '/tmp/libvirthook_ovs_migrate.log'


class VmMigrationHookError(Exception):
    pass


def main(domain, event, phase, stdin=sys.stdin, stdout=sys.stdout, *args):
    if event not in ('migrate', 'restore'):
        sys.exit(0)

    with _logging(_DEBUG_MODE) as log:
        if log:
            print('\nHook input args are:', domain, event, phase, file=log)

        tree = ET.parse(stdin)

        try:
            _process_domxml(tree)
        except:
            traceback.print_exc(file=log)
            raise

        tree.write(stdout)

        if log:
            tree.write(log)
            print('\nEnd of hook', file=log)


def _process_domxml(tree):
    root = tree.getroot()

    vm_uuid = root.find('uuid')
    if vm_uuid is None:
        raise VmMigrationHookError('VM uuid is missing')

    devices = root.find('devices')
    if devices is None:
        raise VmMigrationHookError('VM devices are missing')

    target_vm_conf = _vm_item(_vdscli(), vm_uuid.text)
    if target_vm_conf is None:
        raise VmMigrationHookError('VM lookup failed')
    if 'devices' not in target_vm_conf:
        raise VmMigrationHookError('No devices entity in VM conf')

    _set_graphics(devices, target_vm_conf)
    _set_bridge_interfaces(devices, target_vm_conf)


def _set_bridge_interfaces(devices, target_vm_conf):
    target_vm_nets_by_vnic_mac = {dev['macAddr']: dev['network']
                                  for dev in target_vm_conf['devices']
                                  if dev.get('type') == 'interface'}
    for interface in devices.findall('interface'):
        if interface.get('type') == 'bridge':
            _bind_iface_to_bridge(interface, target_vm_nets_by_vnic_mac)


def _bind_iface_to_bridge(interface, target_vm_nets_by_vnic_mac):
    elem_macaddr = interface.find('mac')
    mac_addr = elem_macaddr.get('address')

    target_vm_net = target_vm_nets_by_vnic_mac[mac_addr]
    target_ovs_bridge = net_api.ovs_bridge(target_vm_net)
    if target_ovs_bridge:
        _bind_iface_to_ovs_bridge(interface, target_ovs_bridge, target_vm_net)
    else:
        _bind_iface_to_linux_bridge(interface, target_vm_net)


def _bind_iface_to_ovs_bridge(interface, target_ovs_bridge, target_vm_net):
    _set_source_bridge(interface, target_ovs_bridge)
    _set_virtualport(interface)

    vlan_tag = net_api.net2vlan(target_vm_net)
    if vlan_tag:
        _set_vlan(interface, vlan_tag)


def _set_vlan(interface, vlan_tag):
    elem_vlan = interface.find('vlan')
    if elem_vlan is None:
        elem_vlan = ET.SubElement(interface, 'vlan')
        elem_tag = ET.SubElement(elem_vlan, 'tag')
    else:
        elem_tag = elem_vlan.find('tag')
    elem_tag.set('id', str(vlan_tag))


def _set_virtualport(interface):
    elem_virtualport = interface.find('virtualport')
    if elem_virtualport is None:
        elem_virtualport = ET.SubElement(interface, 'virtualport')
        elem_virtualport.set('type', 'openvswitch')


def _bind_iface_to_linux_bridge(interface, target_linux_bridge):
    _set_source_bridge(interface, target_linux_bridge)

    elem_virtualport = interface.find('virtualport')
    if elem_virtualport is not None:
        interface.remove(elem_virtualport)

    elem_vlan = interface.find('vlan')
    if elem_vlan is not None:
        interface.remove(elem_vlan)


def _set_source_bridge(interface, bridge):
    elem_source = interface.find('source')
    elem_source.set('bridge', bridge)


def _set_graphics(devices, target_vm_conf):
    # TODO: Support VMs with multiple <graphics> sections.
    graphics = devices.find('graphics')
    if graphics is None:
        return

    graphics_listen = graphics.find('listen')

    target_display_network, target_display_ip = _vmconf_display(target_vm_conf)

    if net_api.ovs_bridge(target_display_network):
        graphics_listen.attrib.pop('network', None)
        graphics_listen.set('type', 'address')
        graphics_listen.set('address', target_display_ip)
    else:
        libvirt_network = net_api.netname_o2l(target_display_network)
        graphics_listen.attrib.pop('address', None)
        graphics_listen.set('type', 'network')
        graphics_listen.set('network', libvirt_network)


def _vmconf_display(vm_conf):
    graphic_devs = [device for device in vm_conf['devices']
                    if device.get('type') == 'graphics']
    for graphic_dev in graphic_devs:
        params = graphic_dev.get('specParams')
        if params and 'displayNetwork' in params and 'displayIp' in params:
            return params['displayNetwork'], params['displayIp']

    raise VmMigrationHookError('VM conf graphics not detected')


def _vm_item(vdscli, vm_uuid):
    result = vdscli.fullList(fullStatus=True, vmList=(vm_uuid,))
    return result['items'][0] if len(result['items']) else None


def _vdscli():
    request_queues = config.get('addresses', 'request_queues')
    request_queue = request_queues.split(',')[0]
    return jsonrpcvdscli.connect(request_queue)


@contextmanager
def _logging(debug_mode=False):
    if debug_mode:
        with open(LOG_FILE, 'a') as log:
            yield log
    else:
        yield None


if __name__ == '__main__':
    main(*sys.argv[1:])
