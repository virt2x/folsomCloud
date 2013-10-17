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

BASE_SQL_CONN=mysql://$MYSQL_GLANCE_USER:$MYSQL_GLANCE_PASSWORD@$MYSQL_HOST

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
nkill glance
[[ -d $DEST/glance ]] && cp -rf $TOPDIR/cloud/glance/etc/* $DEST/glance/etc/
mysql_cmd "DROP DATABASE IF EXISTS glance;"



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
python-netifaces python-netifaces-dbg
service ssh restart

#---------------------------------------------------
# Copy source code to DEST Dir
#---------------------------------------------------

[[ ! -d $DEST ]] && mkdir -p $DEST
if [[ ! -d $DEST/glance ]]; then
    [[ ! -d $DEST/keystone ]] && cp -rf $TOPDIR/cloud/keystone $DEST
    [[ ! -d $DEST/python-keystoneclient ]] && cp -rf $TOPDIR/cloud/python-keystoneclient $DEST/
    [[ ! -d $DEST/swift ]] && cp -rf $TOPDIR/cloud/swift $DEST/
    [[ ! -d $DEST/swift3 ]] && cp -rf $TOPDIR/cloud/swift3 $DEST/
    [[ ! -d $DEST/python-swiftclient ]] && cp -rf $TOPDIR/cloud/python-swiftclient $DEST/
    [[ ! -d $DEST/glance ]] && cp -rf $TOPDIR/cloud/glance $DEST/
    [[ ! -d $DEST/python-glanceclient ]] && cp -rf $TOPDIR/cloud/python-glanceclient $DEST/

    pip install $TOPDIR/pip/pam-0.1.4.tar.gz
    pip install $TOPDIR/pip/WebOb-1.0.8.zip
    pip install $TOPDIR/pip/SQLAlchemy-0.7.9.tar.gz

    source_install python-keystoneclient
    source_install keystone

    source_install python-swiftclient

    pip install $TOPDIR/pip/eventlet-0.9.15.tar.gz
    pip install $TOPDIR/pip/PasteDeploy-1.3.3.tar.gz
    pip install $TOPDIR/pip/Paste-1.7.5.1.tar.gz
    pip install $TOPDIR/pip/simplejson-2.0.9.tar.gz
    pip install $TOPDIR/pip/xattr-0.4.tar.gz
    source_install swift
    source_install swift3

    pip install $TOPDIR/pip/prettytable-0.6.1.zip
    pip install $TOPDIR/pip/warlock-0.6.0.tar.gz
    pip install $TOPDIR/pip/jsonschema-0.7.tar.gz

    source_install python-glanceclient
    pip install $TOPDIR/pip/anyjson-0.3.3.tar.gz
    pip install $TOPDIR/pip/boto-2.1.1.tar.gz
    pip install $TOPDIR/pip/kombu-2.4.10.tar.gz
    pip install $TOPDIR/pip/amqplib-1.0.2.tgz
    source_install glance
fi


#---------------------------------------------------
# Create User in Keystone
#---------------------------------------------------

export SERVICE_TOKEN=$ADMIN_TOKEN
export SERVICE_ENDPOINT=http://$KEYSTONE_HOST:35357/v2.0

get_tenant SERVICE_TENANT service
get_role ADMIN_ROLE admin


if [[ `keystone user-list | grep glance | wc -l` -eq 0 ]]; then
GLANCE_USER=$(get_id keystone user-create \
    --name=glance \
    --pass="$KEYSTONE_GLANCE_SERVICE_PASSWORD" \
    --tenant_id $SERVICE_TENANT \
    --email=glance@example.com)

keystone user-role-add \
    --tenant_id $SERVICE_TENANT \
    --user_id $GLANCE_USER \
    --role_id $ADMIN_ROLE

GLANCE_SERVICE=$(get_id keystone service-create \
    --name=glance \
    --type=image \
    --description="Glance Image Service")

keystone endpoint-create \
    --region RegionOne \
    --service_id $GLANCE_SERVICE \
    --publicurl "http://$GLANCE_HOST:9292/v1" \
    --adminurl "http://$GLANCE_HOST:9292/v1" \
    --internalurl "http://$GLANCE_HOST:9292/v1"

fi

unset SERVICE_TOKEN
unset SERVICE_ENDPOINT

#---------------------------------------------------
# Create glance user in Mysql
#---------------------------------------------------

# create user
cnt=`mysql_cmd "select * from mysql.user;" | grep $MYSQL_GLANCE_USER | wc -l`
if [[ $cnt -eq 0 ]]; then
    mysql_cmd "create user '$MYSQL_GLANCE_USER'@'%' identified by '$MYSQL_GLANCE_PASSWORD';"
    mysql_cmd "flush privileges;"
fi

# create database
cnt=`mysql_cmd "show databases;" | grep glance | wc -l`
if [[ $cnt -eq 0 ]]; then
    mysql_cmd "create database glance CHARACTER SET utf8;"
    mysql_cmd "grant all privileges on glance.* to '$MYSQL_GLANCE_USER'@'%' identified by '$MYSQL_GLANCE_PASSWORD';"
    mysql_cmd "GRANT ALL PRIVILEGES ON *.* TO 'root'@'%' identified by '$MYSQL_ROOT_PASSWORD'; FLUSH PRIVILEGES;"
    mysql_cmd "GRANT ALL PRIVILEGES ON *.* TO 'root'@'%' identified by '$MYSQL_ROOT_PASSWORD'  WITH GRANT OPTION; FLUSH PRIVILEGES;"
    mysql_cmd "flush privileges;"
fi


#################################################
#
# Change configuration file.
#
#################################################

[[ -d /etc/glance ]] && rm -rf /etc/glance/*
mkdir -p /etc/glance

file=/etc/glance/glance-api.conf
cp -rf $TOPDIR/cloud/glance/etc/* /etc/glance/
reg=/etc/glance/glance-registry.conf

# configure for log.
sed -i "s,debug = False,debug = True,g" $file
sed -i "s,debug = False,debug = True,g" $reg
sed -i "s,log_file = /var/log/glance/api.log,log_file = /var/log/nova/glance-api.log,g" $file
sed -i "s,log_file = /var/log/glance/registry.log,log_file = /var/log/nova/glance-registry.log", $reg
mkdir -p /var/log/nova

# for mysql
sed -i "s,sql_connection = sqlite:///glance.sqlite,sql_connection = $BASE_SQL_CONN/glance?charset=utf8,g" $file
sed -i "s,sql_connection = sqlite:///glance.sqlite,sql_connection = $BASE_SQL_CONN/glance?charset=utf8,g" $reg

# rabbitmq
sed -i "s,rabbit_host = localhost,rabbit_host = $RABBITMQ_HOST,g" $file
sed -i "s,notifier_strategy = noop,notifier_strategy = rabbit,g" $file
sed -i "s,rabbit_password = guest,rabbit_password = $RABBITMQ_PASSWORD,g" $file

# Storage dir
sed -i "s,filesystem_store_datadir = /var/lib/glance/images/,filesystem_store_datadir = /opt/stack/data/images/,g" $file
mkdir -p /opt/stack/data/images
sed -i "s,image_cache_dir = /var/lib/glance/image-cache/,image_cache_dir = /opt/stack/data/cache/,g" $file
mkdir -p /opt/stack/data/cache

# Keystone dir
add_line $file "keystone_authtoken" "auth_uri = http://$KEYSTONE_HOST:5000/"
sed -i "s,auth_host = 127.0.0.1,auth_host = $KEYSTONE_HOST,g" $file
sed -i "s,%SERVICE_TENANT_NAME%,$SERVICE_TENANT_NAME,g" $file
sed -i "s,%SERVICE_USER%,glance,g" $file
sed -i "s,%SERVICE_PASSWORD%,$KEYSTONE_GLANCE_SERVICE_PASSWORD,g" $file
add_line $file "paste_deploy" "flavor = keystone+cachemanagement"

add_line $reg "keystone_authtoken" "auth_uri = http://$KEYSTONE_HOST:5000/"
sed -i "s,auth_host = 127.0.0.1,auth_host = $KEYSTONE_HOST,g" $reg
sed -i "s,%SERVICE_TENANT_NAME%,$SERVICE_TENANT_NAME,g" $reg
sed -i "s,%SERVICE_USER%,glance,g" $reg
sed -i "s,%SERVICE_PASSWORD%,$KEYSTONE_GLANCE_SERVICE_PASSWORD,g" $reg
add_line $reg "paste_deploy" "flavor = keystone"


############################################################
#
# SYNC the DataBase.
#
############################################################

glance-manage db_sync --config-file /etc/glance/glance-api.conf


############################################################
#
# Create a script to kill all the services with the name.
#
############################################################


cat <<"EOF" > /root/start.sh
#!/bin/bash
cd /opt/stack/glance
nohup python ./bin/glance-registry --config-file=/etc/glance/glance-registry.conf >/var/log/nova/glance-registry.log 2>&1 &

nohup python ./bin/glance-api      --config-file=/etc/glance/glance-api.conf      >/var/log/nova/glance-api.log  2>&1 &

EOF

chmod +x /root/start.sh
/root/start.sh
rm -rf /tmp/pip*
rm -rf /tmp/tmp*

set +o xtrace
