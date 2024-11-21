#!/usr/bin/python3

import argparse
import requests
import yaml
from pathlib import Path
import sys
from collections import defaultdict

import urllib3
urllib3.disable_warnings()

parser = argparse.ArgumentParser(description='WMF Mini CLab Topology Generator')
parser.add_argument('--netbox', help='Netbox server IP/hostname', type=str, default='netbox.wikimedia.org')
parser.add_argument('-k', '--key', help='Netbox API Token / Key', type=str)
parser.add_argument('--name', help='Name for clab project, file names based on this.', default='wmf-minilab')
parser.add_argument('-l', '--license', help='License file name for crpd if available', type=str)
parser.add_argument('--hosts', help='Comma separated list of hosts to add to the topology', type=str, required=True)
args = parser.parse_args()


def main():
    clab_topo = {
        'name': args.name,
        'mgmt': {
            'network': args.name,
            'bridge': 'clab'
        },
        'topology': {
            'kinds': {
                'nokia_srlinux': { 'image': "ghcr.io/nokia/srlinux:24.7.2" },
                'crpd': { 'image': "crpd:latest" },
                'linux': { 'image': 'debian:clab' }
            },
            'nodes': {},
            'links': []
        },
    }

    lab_device_names = args.hosts.split(",")
    devices = get_devices()
    # Parse data and populate set of unique links between devices in our topology
    links = set()
    device_vendors = {}
    connected_interfaces = defaultdict(list)
    for device in devices:
        device_name = device['name']
        device_vendors[device_name] = device['device_type']['manufacturer']['slug']
        # Add node to topology
        if device['role']['slug'] == 'server':
            clab_topo['topology']['nodes'][device_name] = { 'kind': 'linux' }
        if device['role']['slug'] == 'asw':
            clab_topo['topology']['nodes'][device_name] = { 'kind': 'nokia_srlinux', 'type': 'ixrd2l' }
            device_vendors[device_name] = 'nokia'
        if device['role']['slug'] == 'cr':
            clab_topo['topology']['nodes'][device_name] = { 'kind': 'crpd' }

        # Check the interfaces and populate links
        for interface in device['interfaces']:
            if not (interface['connected_endpoints'] and len(interface['connected_endpoints'][0]) > 0 and 
                    interface['connected_endpoints'][0]['device']['name'] in lab_device_names):
                # Either interface has no connection or its to a node we are not simulating
                continue
            interface_name = interface['name']
            connected_interfaces[device_name].append(interface_name)
            links.add(get_link_tupple(device_name, interface_name,
                      interface['connected_endpoints'][0]['device']['name'],
                      interface['connected_endpoints'][0]['name']))

    # Process all the links recorded and add them to topology in correct format
    for link_tupple in links:
        clab_topo['topology']['links'].append(get_clab_link(link_tupple, device_vendors))

    # Generate output files
    Path("output").mkdir(exist_ok=True)
    generate_start_script(devices, connected_interfaces)
    with open(f'output/{args.name}.yaml', 'w') as outfile:
        yaml.dump(clab_topo, outfile, default_flow_style=False, sort_keys=False)
    

def get_link_tupple(a_dev, a_int, b_dev, b_int) -> dict:
    """ Returns tupple with devices and interfaces in the link, orders
        the interfaces based on device name to ensure we get same 
        tupple regardles of what order the ints are passed """
    if a_dev < b_dev:
        return (a_dev, a_int, b_dev, b_int)
    else:
        return (b_dev, b_int, a_dev, a_int)


def get_clab_link(link_tupple, device_vendors):
    """ Process original link tupple and return in clab format with interface 
        names rewritten as required. """
    clab_link = { 'endpoints': [] }
    for index in (0, 2):
        device_name = link_tupple[index]
        int_name = link_tupple[index+1]
        if device_vendors[device_name] == 'juniper':
            int_name = get_valid_juniper_name(int_name)
        if device_vendors[device_name] == 'nokia':
            int_name = get_nokia_name(int_name)
        clab_link['endpoints'].append(f"{device_name}:{int_name}")

    return clab_link


def get_nokia_name(juniper_name: str) -> str:
    port_num = juniper_name.split('/')[-1]
    return f"e1-{port_num}"


def get_valid_juniper_name(juniper_name: str) -> str:
    return juniper_name.replace('/', '_').replace(":", "_")


def get_devices() -> dict:
    device_query = """
    query clab_devices($devices: [String!]) {
      device_list(filters: {name: { in_list: $devices }}) {
        name
        role { slug }
        device_type { 
          slug
          manufacturer { slug }
        }
        primary_ip4 {
          address
          dns_name
        }
        platform { slug }
        role { slug }
        status
        interfaces {
          name
          type
          parent { name }
          ip_addresses { address }
          connected_endpoints {
            ... on InterfaceType {
              device { name }
              name
            }
          }  
        }
      }
    }
    """
    device_query_vars = {
        "devices": args.hosts.split(",")
    }

    return get_graphql_query(device_query, device_query_vars)['device_list']


def get_graphql_query(query: str, variables: dict = None) -> dict:
    url = f"https://{args.netbox}/graphql/"
    headers = {
        'Authorization': f'Token {args.key}'
    }
    data = {"query": query}
    if variables is not None:
        data['variables'] = variables

    response = requests.post(url=url, headers=headers, json=data)
    response.raise_for_status()
    return response.json()['data']


def generate_start_script(devices, connected_interfaces):
    """ Iterates over devices again generating shell script commands to set up node IP addressing 
        for those that need to be configured directly in Linux """
    with open('output/start.sh', 'w') as outfile:
        for device in devices:
            if device['role']['slug'] == 'asw':
                # ASW running SR-Linux is the only one right now we don't need to add commands for
                continue
            device_name = device['name']
            outfile.write(f"sudo ip netns exec clab-{args.name}-{device_name} " \
                          f"sysctl -w net.ipv4.conf.all.arp_ignore=2\n")
            for interface in device['interfaces']:
                if not interface['ip_addresses']:
                    continue
                interface_name = interface['name']
                if interface_name in connected_interfaces[device_name] or interface_name == "lo0":
                    if device['device_type']['manufacturer']['slug'] == 'juniper':
                        interface_name = get_valid_juniper_name(interface_name)
                    for ip_addr in interface['ip_addresses']:
                        outfile.write(f"sudo ip netns exec clab-{args.name}-{device_name} " \
                                      f"ip addr add {ip_addr['address']} dev {interface_name.replace('lo0', 'lo')}\n")
                elif interface['parent'] and interface['parent']['name'] in connected_interfaces[device_name]:
                    # Juniper sub-interfaces - we need to create sub-interface device, then add IPs
                    interface_name = get_valid_juniper_name(interface_name)
                    parent_name = get_valid_juniper_name(interface['parent']['name'])
                    vlan = int(interface_name.split(".")[-1])
                    # Create device:
                    outfile.write(f"sudo ip netns exec clab-{args.name}-{device_name} " \
                                  f"ip link add link {parent_name} name {interface_name} type vlan id {vlan}\n")
                    # Add IPs:
                    for ip_addr in interface['ip_addresses']:
                        outfile.write(f"sudo ip netns exec clab-{args.name}-{device_name} " \
                                  f"ip addr add {ip_addr['address']} dev {interface_name}\n")
                    # Enable device:
                    outfile.write(f"sudo ip netns exec clab-{args.name}-{device_name} " \
                                  f"ip link set dev {interface_name} up\n")

            outfile.write('\n')


if __name__ == "__main__":
    main()

