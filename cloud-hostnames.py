#!/usr/bin/env python

# This script creates DNS entries for cloud instances and stories copies
# in DynamoDB for quick and easy access.  The following environment variables
# are expected: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION
# and DYNAMODB_TABLE

import argparse
import json
import os
from socket import gethostname
from subprocess import call
from syslog import syslog
import urllib2

from pynamodb.models import Model
from pynamodb.attributes import UnicodeAttribute


class Hostnames(Model):
    """ This class models the DynamoDB table schema. """
    class Meta:
        table_name = os.environ['DYNAMODB_TABLE']
        region = os.environ['AWS_DEFAULT_REGION']
    hostname = UnicodeAttribute(hash_key=True)


def metadata(uri):
    """ Fetches data from the EC2 meta-data API.
    Returns data provided by the API or False on 404.

    Keyword arguments:
    uri -- the API endpoint to query
    """
    API = 'http://169.254.169.254/latest/meta-data'
    try:
        return urllib2.urlopen(url='%s/%s' % (API, uri)).read()
    except urllib2.HTTPError, e:
        if e.code == 404:
            return False
        else:
            raise


def get_hostnames():
    """ Gets the local instance's hostnames from the metadata API.
    Returns a tuple of:
        (vpc_id, public_hostname, private_hostname)
    If vpc_id or public_hostname are not valid, those values will be False.
    """
    mac = metadata('network/interfaces/macs/').strip('/')
    # 404s if not in a VPC
    vpc_id = metadata('network/interfaces/macs/{mac}/vpc-id'.format(mac=mac))
    # 404s if no public IP
    public_hostname = metadata('public-hostname')
    # Should always work
    private_hostname = metadata(
        'network/interfaces/macs/{mac}/local-hostname/'.format(mac=mac))

    return (vpc_id, public_hostname, private_hostname)


def split_hostname(hostname):
    """ Splits a hostname such as my.example.com into my and example.com.
    Returns a tuple of the host (i.e. my) and domain (i.e. example.com).

    Keyword arguments:
    hostname -- A string of the hostname you want to split.
    """
    # Assuming we won't use deeper sub-domains...
    host, domain, tld = hostname.split('.')
    domain = '%s.%s' % (domain, tld)
    return (host, domain)


def run_commands(commands):
    """ Runs the given commands as a subprocess and logs them to syslog.

    Keyword arguments:
    commands -- List of commands to run.
    """
    for command in commands:
        syslog('Running command %s' % command)
        call(command, shell=True)


def rrcreate(ec2_hostname, public=False):
    """ Runs cli53 to create a CNAME for the local host pointing to
    EC2's managed DNS record.  Also creates a second CNAME with dashes removed
    that's easier to type on mobile devices.

    Returns the primary CNAME, but not the one with dashes removed.

    Keyword arguments:
    ec2_hostname -- The instance's hostname provided by EC2.
    public -- Used to indicate a public hostname. If so, pass in True.
    """
    host, domain = split_hostname(gethostname())

    if public:
        host = host + '-public'

    # Add a second record with no dashes that's easier to type on mobile deices
    host_no_dashes = host.replace('-', '')

    cmd = "cli53 rrcreate --replace {domain} '{host} 60 CNAME {ec2_hostname}.'"
    run_commands(cmd.format(domain=domain, host=host,
                            ec2_hostname=ec2_hostname),
                 cmd.format(domain=domain, host=host_no_dashes,
                            ec2_hostname=ec2_hostname))

    return '%s.%s' % (host, domain)


def register(hostnames):
    """
    If a host has a public and private address, we register <nostname>-public
    and <hostname> as independent and unique records. Most of the time if a
    host has a public and private address, we will want to use the private
    address as it's internal and more secure. Therefore, we default to the
    private address as the primary record.

    If there is no public IP, then we only register a private record for
    <hostname>.

    If the host is in EC2-Classic rather than in a VPC, then we only register
    the <hostname> address.

    Returns a dictionary of the records that were added.

    Keyword arguments:
    hostnames -- A tuple of (vpc_id, public_hostname, private_hostname)
    """
    vpc_id, public_hostname, private_hostname = hostnames
    records = []

    if vpc_id:
        if public_hostname:
            records.append(rrcreate(private_hostname))
            records.append(rrcreate(public_hostname, public=True))
        else:
            records.append(rrcreate(private_hostname))
    else:
        records.append(rrcreate(public_hostname, public=True))

    return records


def add_dynamo_hostnames(records):
    """ Inserts the provided values into DynamoDB """
    for hostname in records:
        row = Hostnames(hostname)
        row.save()
    syslog('Added %s to DynamoDB' % records)


def list_dynamo_hostnames():
    """ Lists the hostnames from DynamoDB. """
    dump = json.loads(Hostnames.dumps())
    dump.sort()
    for host in dump:
        print host[0]


def delete(hostname):
    """ Deletes the given hostname from DynamoDB and route53.

    Also deletes any "host-public.domain.tld" records, but in order to look
    for those records in DynamoDB we search for records that begin with
    "host" and make sure they end with "domain.tld", so in theory if you've
    added records that don't follow our normal pattterns the delete could be
    greedy.

    Corresponding route53 records with dashes removed are also deleted.

    Keyword arguments:
    hostname -- The hostname string to delete.  Expects the primary ID,
    not the -public or a stripped string.
    """
    host, domain = split_hostname(hostname)
    cmd = 'cli53 rrdelete {domain} {host} CNAME'
    commands = []

    for row in Hostnames.scan(hostname__begins_with=host):
        # Don't see a way to do a full text scan with pattern matching...
        # So we search the hostname and verify this is the record we want to
        # delete by matching the domain
        if row.hostname.endswith(domain):
            host_to_delete = row.hostname.replace('.%s' % domain, '')
            host_to_delete_no_dashes = host_to_delete.replace('-', '')
            commands.append(cmd.format(domain=domain,
                                       host=host_to_delete))
            commands.append(cmd.format(domain=domain,
                                       host=host_to_delete_no_dashes))
            row.delete()
            syslog('Deleted %s from DynamoDB' % hostname)

    run_commands(commands)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--list', action='store_true',
                        help='List the cloud hostnames from dynamodb')
    parser.add_argument('--delete',
                        help='Delete the given hostname from dynamodb')
    args = parser.parse_args()

    if args.list:
        list_dynamo_hostnames()
    elif args.delete:
        delete(args.delete)
    else:
        hostnames = get_hostnames()
        records = register(hostnames)
        add_dynamo_hostnames(records)
