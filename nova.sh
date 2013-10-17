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

BASE_SQL_CONN=mysql://$MYSQL_NOVA_USER:$MYSQL_NOVA_PASSWORD@$MYSQL_HOST

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
nkill nova
[[ -d $DEST/nova ]] && cp -rf $TOPDIR/cloud/nova/etc/nova/* $DEST/nova/etc/nova/
mysql_cmd "DROP DATABASE IF EXISTS nova;"

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
dnsmasq-base dnsmasq-base dnsmasq-utils kpartx parted \
iputils-arping python-mysqldb python-xattr \
python-lxml kvm gawk iptables ebtables sqlite3 sudo kvm \
vlan curl socat python-mox  \
python-migrate python-gflags python-greenlet python-libxml2 \
python-routes python-netaddr  python-eventlet \
python-cheetah python-carrot python-tempita python-sqlalchemy \
python-suds python-lockfile python-m2crypto python-boto python-kombu \
python-feedparser python-iso8601 python-qpid
service ssh restart

#---------------------------------------------------
# Copy source code to DEST Dir
#---------------------------------------------------

[[ ! -d $DEST ]] && mkdir -p $DEST
if [[ ! -d $DEST/nova ]]; then
    [[ ! -d $DEST/keystone ]] && cp -rf $TOPDIR/cloud/keystone $DEST
    [[ ! -d $DEST/python-keystoneclient ]] && cp -rf $TOPDIR/cloud/python-keystoneclient $DEST/
    [[ ! -d $DEST/swift ]] && cp -rf $TOPDIR/cloud/swift $DEST/
    [[ ! -d $DEST/swift3 ]] && cp -rf $TOPDIR/cloud/swift3 $DEST/
    [[ ! -d $DEST/python-swiftclient ]] && cp -rf $TOPDIR/cloud/python-swiftclient $DEST/
    [[ ! -d $DEST/glance ]] && cp -rf $TOPDIR/cloud/glance $DEST/
    [[ ! -d $DEST/python-glanceclient ]] && cp -rf $TOPDIR/cloud/python-glanceclient $DEST/
    [[ ! -d $DEST/python-quantumclient ]] && cp -rf $TOPDIR/cloud/python-quantumclient $DEST/
    [[ ! -d $DEST/quantum ]] && cp -rf $TOPDIR/cloud/quantum $DEST/

    pip_install pam-0.1.4.tar.gz
    pip_install WebOb-1.0.8.zip
    pip_install SQLAlchemy-0.7.9.tar.gz

    source_install python-keystoneclient
    source_install keystone

    source_install python-swiftclient

    pip_install eventlet-0.9.17.tar.gz
    #pip_install PasteDeploy-1.3.3.tar.gz
    pip_install Paste-1.7.5.1.tar.gz
    pip_install simplejson-2.0.9.tar.gz
    pip_install xattr-0.4.tar.gz
    source_install swift
    source_install swift3

    pip_install prettytable-0.6.1.zip
    pip_install jsonschema-0.7.tar.gz
    pip_install warlock-0.6.0.tar.gz

    source_install python-glanceclient
    pip_install anyjson-0.3.3.tar.gz
    pip_install boto-2.1.1.tar.gz
    pip_install amqplib-1.0.2.tgz
    pip_install kombu-2.4.10.tar.gz
    source_install glance


    pip_install pyparsing-1.5.6.zip
    pip_install cmd2-0.6.4.tar.gz
    pip_install cliff-1.3.tar.gz
    pip_install PasteDeploy-1.5.0.tar.gz
    source_install python-quantumclient

    pip_install kombu-1.0.4.tar.gz
    pip_install amqplib-0.6.1.tgz
    source_install quantum

    source_install python-novaclient
    pip_install PasteDeploy-1.5.0.tar.gz
    pip_install Paste-1.7.5.1.tar.gz
fi

#---------------------------------------------------
# Create User in Keystone
#---------------------------------------------------

export SERVICE_TOKEN=$ADMIN_TOKEN
export SERVICE_ENDPOINT=http://$KEYSTONE_HOST:35357/v2.0

get_tenant SERVICE_TENANT service
get_role ADMIN_ROLE admin


if [[ `keystone user-list | grep nova | wc -l` -eq 0 ]]; then
NOVA_USER=$(get_id keystone user-create \
    --name=nova \
    --pass="$KEYSTONE_NOVA_SERVICE_PASSWORD" \
    --tenant_id $SERVICE_TENANT \
    --email=nova@example.com)
keystone user-role-add \
    --tenant_id $SERVICE_TENANT \
    --user_id $NOVA_USER \
    --role_id $ADMIN_ROLE
NOVA_SERVICE=$(get_id keystone service-create \
    --name=nova \
    --type=compute \
    --description="Nova Compute Service")
keystone endpoint-create \
    --region RegionOne \
    --service_id $NOVA_SERVICE \
    --publicurl "http://$NOVA_HOST:\$(compute_port)s/v2/\$(tenant_id)s" \
    --adminurl "http://$NOVA_HOST:\$(compute_port)s/v2/\$(tenant_id)s" \
    --internalurl "http://$NOVA_HOST:\$(compute_port)s/v2/\$(tenant_id)s"
RESELLER_ROLE=$(get_id keystone role-create --name=ResellerAdmin)
keystone user-role-add \
    --tenant_id $SERVICE_TENANT \
    --user_id $NOVA_USER \
    --role_id $RESELLER_ROLE

EC2_SERVICE=$(get_id keystone service-create \
    --name=ec2 \
    --type=ec2 \
    --description="EC2 Compatibility Layer")
keystone endpoint-create \
    --region RegionOne \
    --service_id $EC2_SERVICE \
    --publicurl "http://$NOVA_HOST:8773/services/Cloud" \
    --adminurl "http://$NOVA_HOST:8773/services/Admin" \
    --internalurl "http://$NOVA_HOST:8773/services/Cloud"
fi

unset SERVICE_TOKEN
unset SERVICE_ENDPOINT

#---------------------------------------------------
# Create glance user in Mysql
#---------------------------------------------------

# create user
cnt=`mysql_cmd "select * from mysql.user;" | grep $MYSQL_NOVA_USER | wc -l`
if [[ $cnt -eq 0 ]]; then
    mysql_cmd "create user '$MYSQL_NOVA_USER'@'%' identified by '$MYSQL_NOVA_PASSWORD';"
    mysql_cmd "flush privileges;"
fi

# create database
cnt=`mysql_cmd "show databases;" | grep nova | wc -l`
if [[ $cnt -eq 0 ]]; then
    mysql_cmd "create database nova CHARACTER SET latin1;"
    mysql_cmd "grant all privileges on nova.* to '$MYSQL_NOVA_USER'@'%' identified by '$MYSQL_NOVA_PASSWORD';"
    mysql_cmd "GRANT ALL PRIVILEGES ON *.* TO 'root'@'%' identified by '$MYSQL_ROOT_PASSWORD'; FLUSH PRIVILEGES;"
    mysql_cmd "GRANT ALL PRIVILEGES ON *.* TO 'root'@'%' identified by '$MYSQL_ROOT_PASSWORD'  WITH GRANT OPTION; FLUSH PRIVILEGES;"
    mysql_cmd "flush privileges;"
fi

#################################################
#
# Change configuration file.
#
#################################################

[[ -d /etc/nova ]] && rm -rf /etc/nova/*
mkdir -p /etc/nova
cp -rf $TOPDIR/cloud/nova/etc/nova/* /etc/nova/


file=/etc/nova/nova.conf
cp -rf $TOPDIR/nova.conf $file
HOST_IP=$NOVA_HOST

sed -i "s,%HOST_IP%,$HOST_IP,g" $file
sed -i "s,%GLANCE_HOST%,$GLANCE_HOST,g" $file
sed -i "s,%MYSQL_NOVA_USER%,$MYSQL_NOVA_USER,g" $file
sed -i "s,%MYSQL_NOVA_PASSWORD%,$MYSQL_NOVA_PASSWORD,g" $file
sed -i "s,%MYSQL_HOST%,$MYSQL_HOST,g" $file
sed -i "s,%NOVA_HOST%,$NOVA_HOST,g" $file
sed -i "s,%KEYSTONE_QUANTUM_SERVICE_PASSWORD%,$KEYSTONE_QUANTUM_SERVICE_PASSWORD,g" $file
sed -i "s,%KEYSTONE_HOST%,$KEYSTONE_HOST,g" $file
sed -i "s,%QUANTUM_HOST%,$QUANTUM_HOST,g" $file
sed -i "s,%DASHBOARD_HOST%,$DASHBOARD_HOST,g" $file
sed -i "s,%RABBITMQ_HOST%,$RABBITMQ_HOST,g" $file
sed -i "s,%RABBITMQ_PASSWORD%,$RABBITMQ_PASSWORD,g" $file
sed -i "s,%LIBVIRT_TYPE%,$LIBVIRT_TYPE,g" $file

file=/etc/nova/api-paste.ini
sed -i "s,auth_host = 127.0.0.1,auth_host = $KEYSTONE_HOST,g" $file
sed -i "s,%SERVICE_TENANT_NAME%,$SERVICE_TENANT_NAME,g" $file
sed -i "s,%SERVICE_USER%,nova,g" $file
sed -i "s,%SERVICE_PASSWORD%,$KEYSTONE_NOVA_SERVICE_PASSWORD,g" $file

file=/etc/nova/rootwrap.conf
sed -i "s,filters_path=.*,filters_path=/etc/nova/rootwrap.d,g" $file

############################################################
#
# SYNC the DataBase.
#
############################################################

nova-manage db version
nova-manage db sync


############################################################
#
# Create a script to kill all the services with the name.
#
############################################################


cat <<"EOF" > /root/start.sh
#!/bin/bash
cd /opt/stack/nova
mkdir -p /var/log/nova
mkdir -p /opt/stack/data/nova
mkdir -p /opt/stack/data/nova/instances
nohup python ./bin/nova-api --config-file=/etc/nova/nova.conf >/var/log/nova/nova-api.log 2>&1 &
nohup python ./bin/nova-cert --config-file=/etc/nova/nova.conf >/var/log/nova/nova-cert.log 2>&1 &
nohup python ./bin/nova-scheduler --config-file=/etc/nova/nova.conf >/var/log/nova/nova-sche.log 2>&1 &
nohup python ./bin/nova-consoleauth --config-file=/etc/nova/nova.conf >/var/log/nova/nova-ca.log 2>&1 &
EOF

chmod +x /root/start.sh
/root/start.sh
rm -rf /tmp/pip*
rm -rf /tmp/tmp*

set +o xtrace
