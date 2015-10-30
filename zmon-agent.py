import os
import argparse
import boto.iam
import boto.ec2
import boto.ec2.elb
import boto.ec2.instance
import boto.cloudformation
import boto.utils
import boto.rds2
import json
import base64
import yaml
import requests
import hashlib
from pprint import pprint

import string
BASE_LIST = string.digits + string.letters
BASE_DICT = dict((c, i) for i, c in enumerate(BASE_LIST))

def base_decode(string, reverse_base=BASE_DICT):
    length = len(reverse_base)
    ret = 0
    for i, c in enumerate(string[::-1]):
        ret += (length ** i) * reverse_base[c]

    return ret

def base_encode(integer, base=BASE_LIST):
    length = len(base)
    ret = ''
    while integer != 0:
        ret = base[integer % length] + ret
        integer /= length

    return ret

def get_hash(ip):
    m = hashlib.sha256()
    m.update(ip)
    h = m.hexdigest()
    h = base_encode(int(h[10:18], 16))
    return h

def get_running_apps(region):
    aws = boto.ec2.connect_to_region(region)
    rs = aws.get_all_reservations()
    result = []

    for r in rs:

        owner = r.owner_id

        instances = r.instances

        for i in instances:

            if str(i._state) != 'running(16)':
                continue

            try:
                user_data = base64.b64decode(str(i.get_attribute('userData')["userData"]))
                user_data = yaml.load(user_data)
            except Exception as ex:
               pass

            # for now limit us to instances with valid user data ( senza/taupage )
            if isinstance(user_data, dict) and 'application_id' in user_data:
                ins = {'type':'instance', 'created_by':'agent'}
                ins['state_reason'] = i.state_reason
                ins['events'] = i.eventsSet
                ins['id'] = '{}-{}-{}[aws:{}:{}]'.format(user_data['application_id'], user_data['application_version'], get_hash(i.private_ip_address+""), owner, region)
                ins['instance_type'] = i.instance_type
                ins['aws_id']=i.id

                ins['application_id'] = user_data['application_id']
                ins['application_version'] = user_data['application_version']
                ins['source'] = user_data['source']

                if 'ports' in user_data:
                    ins['ports'] = user_data['ports']

                ins['runtime'] = user_data['runtime']
                ins['ip'] = i.private_ip_address
                ins['host'] = i.private_dns_name
                ins['infrastructure_account'] = 'aws:{}'.format(owner)
                ins['region'] = region

                if i.tags:
                    if 'StackVersion' in i.tags:
                        ins['stack'] = i.tags['Name']
                        ins['resource_id'] = i.tags['aws:cloudformation:logical-id']

                    if "Name" in i.tags and 'cassandra' in i.tags['Name'] and 'opscenter' not in i.tags['Name']:
                        cas = ins.copy()
                        cas['type'] = 'cassandra'
                        cas['id'] = "cas-{}".format(cas['id'])
                        result.append(cas)

                result.append(ins)

            else:

                ins = {'type':'instance', 'created_by':'agent'}
                ins['id'] = '{}-{}[aws:{}:{}]'.format(i.id, get_hash(i.private_ip_address+""), owner, region)
                ins['infrastructure_account'] = 'aws:{}'.format(owner)
                ins['ip'] = i.private_ip_address
                ins['host'] = i.private_dns_name
                ins['region'] = region
                ins['instance_type'] = i.instance_type
                ins['aws_id']=i.id

                if i.tags:
                    if 'Name' in i.tags:
                        ins['name'] = i.tags['Name'].replace(" ","-")

                result.append(ins)

    return result

def get_running_elbs(region, acc):
    aws = boto.ec2.elb.connect_to_region(region)

    elbs = aws.get_all_load_balancers()

    lbs = []

    for e in elbs:
        lb = {'type':'elb', 'infrastructure_account':acc, 'region': region, 'created_by':'agent'}
        lb['id'] = 'elb-{}[{}:{}]'.format(e.name, acc, region)
        lb['dns_name'] = e.dns_name
        lb['host'] = e.dns_name
        lb['name'] = e.name
        lb['scheme'] = e.scheme

        stack = e.name.rsplit('-', 1)
        lb['stack_name'] = stack[0]
        lb['stack_version'] = stack[-1]

        lb['url'] = 'https://{}'.format(lb['host'])
        lb['region'] = region
        lb['members'] = len(e.instances)
        lbs.append(lb)

        ihealth = aws.describe_instance_health(load_balancer_name=e.name)
        in_service = 0
        for ih in ihealth:
            if ih.state == 'InService':
                in_service += 1

        lb['active_members'] = in_service

    return lbs


def get_account_alias(region):
    try:
        c = boto.iam.connect_to_region(region)
        resp = c.get_account_alias()
        return resp['list_account_aliases_response']['list_account_aliases_result']['account_aliases'][0]
    except:
        return None

def get_apps_from_entities(instances, account, region):
    apps = set()
    for i in instances:
        if 'application_id' in i:
            apps.add(i['application_id'])

    applications = []
    for a in apps:
        applications.append({"id":"a-{}[{}:{}]".format(a, account, region), "application_id":a, "region":region, "infrastructure_account":account, "type":"application", "created_by":"agent"})

    return applications

def get_rds_instances(region, acc):
    rds_instances = []

    try:
        aws = boto.rds2.connect_to_region(region)
        instances = aws.describe_db_instances()
        for i in instances["DescribeDBInstancesResponse"]["DescribeDBInstancesResult"]["DBInstances"]:
            
            db = {"id":"rds-{}[{}]".format(i["DBInstanceIdentifier"],acc), "created_by":"agent","infrastructure_account":"{}".format(acc)}
            
            db["type"] = "database"
            db["engine"] = i["Engine"]
            db["port"] = i["Endpoint"]["Port"]
            db["host"] = i["Endpoint"]["Address"]
            db["name"] = i["DBInstanceIdentifier"]
            db["region"] = region

            if "EngineVersion" in i:
                db["version"] = i["EngineVersion"]

            cluster_name = db["name"]
            if "DBName" in i and i["DBName"] != None and i["DBName"] != "":
                cluster_name = i["DBName"]

            db["shards"]={cluster_name: "{}:{}/{}".format(db["host"], db["port"], cluster_name)}

            rds_instances.append(db)

    except Exception as ex:
        print ex

    return rds_instances



def main():
    argp = argparse.ArgumentParser(description='ZMon AWS Agent')
    argp.add_argument('-e', '--entity-service', dest='entityservice')
    argp.add_argument('-r', '--region', dest='region', default=None)
    argp.add_argument('-j', '--json', dest='json', action='store_true')
    args = argp.parse_args()

    if args.region == None:
        print "Trying to figure out region..."
        region = boto.utils.get_instance_metadata()['placement']['availability-zone'][:-1]
    else:
        region = args.region

    print "Using region: {}".format(region)

    print "Entity service url: ", args.entityservice

    apps = get_running_apps(region)
    if len(apps) > 0:
        infrastructure_account = apps[0]['infrastructure_account']
        elbs = get_running_elbs(region, infrastructure_account)
        rds = get_rds_instances(region, infrastructure_account)
    else:
        elbs = []
        rds = []

    if args.json:
        d = {'apps':apps, 'elbs':elbs}
        print json.dumps(d)
    else:

        if infrastructure_account is not None:
            account_alias = get_account_alias(region)
            ia_entity = { "type": "local",
                          "infrastructure_account": infrastructure_account,
                          "account_alias": account_alias,
                          "region": region,
                          "id": "aws-ac[{}:{}]".format(infrastructure_account, region),
                          "created_by": "agent" }

            application_entities = get_apps_from_entities(apps, infrastructure_account, region)

            current_entities = []

            for e in elbs:
                current_entities.append(e["id"])

            for a in apps:
                current_entities.append(a["id"])

            for a in application_entities:
                current_entities.append(a["id"])

            for a in rds:
                current_entities.append(a["id"])

            current_entities.append(ia_entity["id"])

            # removing all entities
            r = requests.get(args.entityservice, params={'query':'{"infrastructure_account": "'+infrastructure_account+'", "region": "'+region+'", "created_by": "agent"}'})
            entities = r.json()

            existing_entities = {}

            to_remove = []
            for e in entities:
                existing_entities[e['id']] = e
                if not e["id"] in current_entities:
                    to_remove.append(e["id"])

            if os.getenv('zmon_user'):
                auth = (os.getenv('zmon_user'), os.getenv('zmon_password', ''))
            else:
                auth = None

            for e in to_remove:
                print "removing instance: {}".format(e)

                r = requests.delete(args.entityservice+"{}/".format(e), auth=auth)

                print "...", r.status_code

            def put_entity(entity_type, entity):
                print "Adding {} entity: {}".format(entity_type, entity['id'])

                r = requests.put(args.entityservice, auth=auth,
                                 data=json.dumps(entity),
                                 headers={'content-type':'application/json'})

                print "...", r.status_code

            put_entity('LOCAL', ia_entity)

            for instance in apps:
                put_entity('instance', instance)

            for elb in elbs:
                put_entity('elastic load balancer', elb)

            for db in rds:
                put_entity('RDS instance', db)

            # merge here or we loose it on next pull
            for app in application_entities:
                if app['id'] in existing_entities:
                    ex = existing_entities[app['id']]
                    if 'scalyr_ts_id' in ex:
                        app['scalyr_ts_id'] = ex['scalyr_ts_id']

            for app in application_entities:
                put_entity('application', app)

if __name__ == '__main__':
    main()