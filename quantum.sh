#!/bin/bash

set -e
set -o xtrace

TOPDIR=$(cd $(dirname "$0") && pwd)
TEMP=`mktemp`;
rm -rfv $TEMP >/dev/null;
mkdir -p $TEMP;
source $TOPDIR/localrc
source $TOPDIR/tools/function
DEST=/opt/stack/

###########################################################
#
#  Your Configurations.
#
###########################################################

BASE_SQL_CONN=mysql://$MYSQL_QUANTUM_USER:$MYSQL_QUANTUM_PASSWORD@$MYSQL_HOST

unset OS_USERNAME
unset OS_AUTH_KEY
unset OS_AUTH_TENANT
unset OS_STRATEGY
unset OS_AUTH_STRATEGY
unset OS_AUTH_URL
unset SERVICE_TOKEN
unset SERVICE_ENDPOINT

KEYSTONE_AUTH_HOST=$KEYSTONE_HOST
KEYSTONE_AUTH_PORT=35357
KEYSTONE_AUTH_PROTOCOL=http
KEYSTONE_SERVICE_HOST=$KEYSTONE_HOST
KEYSTONE_SERVICE_PORT=5000
KEYSTONE_SERVICE_PROTOCOL=http
SERVICE_ENDPOINT=http://$KEYSTONE_HOST:35357/v2.0

#---------------------------------------------------
# Clear Front installation
#---------------------------------------------------

DEBIAN_FRONTEND=noninteractive \
apt-get --option \
"Dpkg::Options::=--force-confold" --assume-yes \
install -y --force-yes mysql-client

nkill quantum

[[ -d $DEST/quantum ]] && cp -rf $TOPDIR/cloud/quantum/etc/* $DEST/quantum/etc/
mysql_cmd "DROP DATABASE IF EXISTS quantum;"



############################################################
#
# Install some basic used debs.
#
############################################################

apt-get install -y --force-yes openssh-server build-essential git \
python-dev python-setuptools python-pip \
libxml2-dev libxslt-dev python-pam python-lxml \
python-iso8601 python-sqlalchemy python-migrate \
python-routes  python-passlib \
python-greenlet python-eventlet unzip python-prettytable \
python-mysqldb mysql-client memcached openssl expect \
python-netifaces python-netifaces-dbg \
make fakeroot dkms openvswitch-switch openvswitch-datapath-dkms \
ebtables iptables iputils-ping iputils-arping sudo python-boto \
python-iso8601 python-routes python-suds python-netaddr \
 python-greenlet python-kombu python-eventlet \
python-sqlalchemy python-mysqldb python-pyudev python-qpid dnsmasq-base \
dnsmasq-utils vlan
service ssh restart

#---------------------------------------------------
# Copy source code to DEST Dir
#---------------------------------------------------

[[ ! -d $DEST ]] && mkdir -p $DEST
if [[ ! -d $DEST/quantum ]]; then
    [[ ! -d $DEST/keystone ]] && cp -rf $TOPDIR/cloud/keystone $DEST/
    [[ ! -d $DEST/python-keystoneclient ]] && cp -rf $TOPDIR/cloud/python-keystoneclient $DEST/
    [[ ! -d $DEST/quantum ]] && cp -rf $TOPDIR/cloud/quantum $DEST/
    [[ ! -d $DEST/python-quantumclient ]] && cp -rf $TOPDIR/cloud/python-quantumclient $DEST/

    pip install $TOPDIR/pip/pam-0.1.4.tar.gz
    pip install $TOPDIR/pip/WebOb-1.0.8.zip
    pip install $TOPDIR/pip/SQLAlchemy-0.7.9.tar.gz

    source_install python-keystoneclient
    source_install keystone

    pip install $TOPDIR/pip/cliff-1.3.tar.gz
    pip install $TOPDIR/pip/prettytable-0.6.1.zip
    pip install $TOPDIR/pip/pyparsing-1.5.6.zip
    pip install $TOPDIR/pip/cmd2-0.6.4.tar.gz

    source_install python-quantumclient

    pip install $TOPDIR/pip/amqplib-0.6.1.tgz
    pip install $TOPDIR/pip/eventlet-0.9.17.tar.gz
    pip install $TOPDIR/pip/kombu-1.0.4.tar.gz

    source_install quantum
fi


#---------------------------------------------------
# Create User in Keystone
#---------------------------------------------------

export SERVICE_TOKEN=$ADMIN_TOKEN
export SERVICE_ENDPOINT=http://$KEYSTONE_HOST:35357/v2.0

get_tenant SERVICE_TENANT service
get_role ADMIN_ROLE admin


if [[ `keystone user-list | grep quantum | wc -l` -eq 0 ]]; then
QUANTUM_USER=$(get_id keystone user-create \
    --name=quantum \
    --pass="$KEYSTONE_QUANTUM_SERVICE_PASSWORD" \
    --tenant_id $SERVICE_TENANT \
    --email=quantum@example.com)
keystone user-role-add \
    --tenant_id $SERVICE_TENANT \
    --user_id $QUANTUM_USER \
    --role_id $ADMIN_ROLE
QUANTUM_SERVICE=$(get_id keystone service-create \
    --name=quantum \
    --type=network \
    --description="Quantum Service")
keystone endpoint-create \
    --region RegionOne \
    --service_id $QUANTUM_SERVICE \
    --publicurl "http://$QUANTUM_HOST:9696/" \
    --adminurl "http://$QUANTUM_HOST:9696/" \
    --internalurl "http://$QUANTUM_HOST:9696/"
fi

unset SERVICE_TOKEN
unset SERVICE_ENDPOINT

#---------------------------------------------------
# Create quantum user in Mysql
#---------------------------------------------------

# create user
cnt=`mysql_cmd "select * from mysql.user;" | grep $MYSQL_QUANTUM_USER | wc -l`
if [[ $cnt -eq 0 ]]; then
    mysql_cmd "create user '$MYSQL_QUANTUM_USER'@'%' identified by '$MYSQL_QUANTUM_PASSWORD';"
    mysql_cmd "flush privileges;"
fi

# create database
cnt=`mysql_cmd "show databases;" | grep quantum | wc -l`
if [[ $cnt -eq 0 ]]; then
    mysql_cmd "create database quantum CHARACTER SET utf8;"
    mysql_cmd "grant all privileges on quantum.* to '$MYSQL_QUANTUM_USER'@'%' identified by '$MYSQL_QUANTUM_PASSWORD';"
    mysql_cmd "GRANT ALL PRIVILEGES ON *.* TO 'root'@'%' identified by '$MYSQL_ROOT_PASSWORD'; FLUSH PRIVILEGES;"
    mysql_cmd "GRANT ALL PRIVILEGES ON *.* TO 'root'@'%' identified by '$MYSQL_ROOT_PASSWORD'  WITH GRANT OPTION; FLUSH PRIVILEGES;"
    mysql_cmd "flush privileges;"
fi

#################################################
#
# Change configuration file.
#
#################################################

[[ -d /etc/quantum ]] && rm -rf /etc/quantum/*
mkdir -p /etc/quantum
cp -rf $DEST/quantum/etc/* /etc/quantum/
mv /etc/quantum/quantum/plugins /etc/quantum
mv /etc/quantum/quantum/rootwrap.d /etc/quantum/
rm -rf /etc/quantum/quantum


file=/etc/quantum/api-paste.ini
sed -i "s,auth_host = 127.0.0.1,auth_host = $KEYSTONE_HOST,g" $file
sed -i "s,%SERVICE_TENANT_NAME%,$SERVICE_TENANT_NAME,g" $file
sed -i "s,%SERVICE_USER%,quantum,g" $file
sed -i "s,%SERVICE_PASSWORD%,$KEYSTONE_QUANTUM_SERVICE_PASSWORD,g" $file

file=/etc/quantum/dhcp_agent.ini
sed -i "1a admin_password = $KEYSTONE_QUANTUM_SERVICE_PASSWORD" $file
sed -i "2a admin_user = quantum" $file
sed -i "3a admin_tenant_name = $SERVICE_TENANT_NAME" $file
sed -i "4a auth_url = http://$KEYSTONE_HOST:35357/v2.0" $file
sed -i "5a use_namespaces = $USING_NAMESPACE" $file
sed -i "6a debug = True" $file
sed -i "7a verbose = True" $file
sed -i "s,root_helper = sudo,root_helper = $DEST/quantum/bin/quantum-rootwrap /etc/quantum/rootwrap.conf,g" $file

file=/etc/quantum/l3_agent.ini
sed -i "1a external_network_bridge = br-ex" $file
sed -i "2a use_namespaces = $USING_NAMESPACE" $file
sed -i "3a metadata_ip = $NOVA_HOST" $file
sed -i "4a debug = True" $file
sed -i "5a verbose = True" $file
sed -i "s,auth_url = http://localhost:35357/v2.0,auth_url = http://$KEYSTONE_HOST:35357/v2.0,g" $file
sed -i "s,auth_host = 127.0.0.1,auth_host = $KEYSTONE_HOST,g" $file
sed -i "s,%SERVICE_TENANT_NAME%,$SERVICE_TENANT_NAME,g" $file
sed -i "s,%SERVICE_USER%,quantum,g" $file
sed -i "s,%SERVICE_PASSWORD%,$KEYSTONE_QUANTUM_SERVICE_PASSWORD,g" $file
sed -i "s,root_helper = sudo,root_helper = $DEST/quantum/bin/quantum-rootwrap /etc/quantum/rootwrap.conf,g" $file

file=/etc/quantum/plugins/openvswitch/ovs_quantum_plugin.ini
sed -i "s,sql_connection = sqlite://,sql_connection = mysql://$MYSQL_QUANTUM_USER:$MYSQL_QUANTUM_PASSWORD@$MYSQL_HOST/quantum?charset=utf8,g" $file
sed -i "14a bridge_mappings = phynet1:br-eth1" $file
sed -i "15a network_vlan_ranges = phynet1:1:4094" $file
sed -i "16a tenant_network_type = vlan" $file
sed -i "s,root_helper = sudo,root_helper =  /opt/stack/quantum/bin/quantum-rootwrap /etc/quantum/rootwrap.conf,g" $file

file=/etc/quantum/quantum.conf
sed -i "s,core_plugin = quantum.plugins.sample.SamplePlugin.FakePlugin,core_plugin = quantum.plugins.openvswitch.ovs_quantum_plugin.OVSQuantumPluginV2,g" $file
sed -i "1a rabbit_password = $RABBITMQ_PASSWORD" $file
sed -i "2a rabbit_host = $RABBITMQ_HOST" $file
sed -i "3a auth_strategy = keystone" $file
sed -i "s,api_paste_config = api-paste.ini,api_paste_config = /etc/quantum/api-paste.ini,g" $file


############################################################
#
# SYNC the DataBase.
#
############################################################

cnt=`ovs-vsctl show | grep "br-int" | wc -l`
if [[ $cnt -eq 0 ]]; then
    ovs-vsctl add-br br-eth1
    ovs-vsctl add-port br-eth1 eth1
    ovs-vsctl add-br br-int
    ovs-vsctl add-br br-ex
fi



############################################################
#
# Create a script to kill all the services with the name.
#
############################################################


cat <<"EOF" > /root/start.sh
#!/bin/bash

mkdir -p /var/log/nova/

nohup python /opt/stack/quantum/bin/quantum-server --config-file /etc/quantum/quantum.conf --config-file /etc/quantum/plugins/openvswitch/ovs_quantum_plugin.ini > /var/log/nova/quantum-server.log 2>&1 &

nohup python /opt/stack/quantum/bin/quantum-dhcp-agent --config-file /etc/quantum/quantum.conf --config-file=/etc/quantum/dhcp_agent.ini >/var/log/nova/quantum-dhcp.log 2>&1 &

nohup python /opt/stack/quantum/bin/quantum-l3-agent --config-file /etc/quantum/quantum.conf --config-file=/etc/quantum/l3_agent.ini > /var/log/nova/quantum-l3.log 2>&1 &


nohup python /opt/stack/quantum/bin/quantum-openvswitch-agent --config-file /etc/quantum/quantum.conf --config-file /etc/quantum/plugins/openvswitch/ovs_quantum_plugin.ini > /var/log/nova/quantum-ovs.log 2>&1 &

EOF

chmod +x /root/start.sh
/root/start.sh
rm -rf /tmp/pip*
rm -rf /tmp/tmp*

set +o xtrace
