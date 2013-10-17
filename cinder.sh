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

BASE_SQL_CONN=mysql://$MYSQL_CINDER_USER:$MYSQL_CINDER_PASSWORD@$MYSQL_HOST

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

nkill cinder
[[ -d $DEST/cinder ]] && cp -rf $TOPDIR/cloud/cinder/etc/cinder/* $DEST/cinder/etc/cinder/
mysql_cmd "DROP DATABASE IF EXISTS cinder;"

############################################################
#
# Install some basic used debs.
#
############################################################



apt-get install -y --force-yes openssh-server build-essential git \
python-dev python-setuptools python-pip \
libxml2-dev libxslt-dev tgt lvm2 python-pam python-lxml \
python-iso8601 python-sqlalchemy python-migrate \
unzip python-mysqldb mysql-client memcached openssl expect \
iputils-arping python-xattr \
python-lxml kvm gawk iptables ebtables sqlite3 sudo kvm \
vlan curl socat python-mox  \
python-migrate python-gflags python-greenlet python-libxml2 \
iscsitarget iscsitarget-dkms open-iscsi


service ssh restart

#---------------------------------------------------
# Copy source code to DEST Dir
#---------------------------------------------------

[[ ! -d $DEST ]] && mkdir -p $DEST
if [[ ! -d $DEST/cinder ]]; then
    [[ ! -d $DEST/cinder ]] && cp -rf $TOPDIR/cloud/cinder $DEST/
    [[ ! -d $DEST/python-glanceclient ]] && cp -rf $TOPDIR/cloud/python-glanceclient $DEST/
    [[ ! -d $DEST/keystone ]] && cp -rf $TOPDIR/cloud/keystone $DEST/
    [[ ! -d $DEST/python-cinderclient ]] && cp -rf $TOPDIR/cloud/python-cinderclient $DEST/

    pip_install prettytable-0.6.1.zip
    pip_install WebOb-1.0.8.zip
    pip_install SQLAlchemy-0.7.9.tar.gz

    source_install python-keystoneclient
    pip_install warlock-0.7.0.tar.gz
    pip_install jsonschema-0.7.zip
    pip_install jsonpatch-0.10.tar.gz
    pip_install jsonpointer-0.5.tar.gz

    source_install python-glanceclient

    pip_install amqplib-0.6.1.tgz
    pip_install anyjson-0.3.3.tar.gz
    pip_install eventlet-0.9.17.tar.gz
    pip_install kombu-1.0.4.tar.gz
    pip_install lockfile-0.8.tar.gz
    pip_install python-daemon-1.5.5.tar.gz
    pip_install Routes-1.12.3.tar.gz
    pip_install PasteDeploy-1.5.0.tar.gz
    pip_install Paste-1.7.5.1.tar.gz
    pip_install suds-0.4.tar.gz
    pip_install paramiko-1.9.0.tar.gz
    pip_install Babel-0.9.6.tar.gz

    source_install cinder
    pip_install pam-0.1.4.tar.gz
    pip_install passlib-1.6.1.tar.gz
    source_install keystone
    source_install python-cinderclient
fi

#---------------------------------------------------
# Create User in Keystone
#---------------------------------------------------

export SERVICE_TOKEN=$ADMIN_TOKEN
export SERVICE_ENDPOINT=http://$KEYSTONE_HOST:35357/v2.0

get_tenant SERVICE_TENANT service
get_role ADMIN_ROLE admin


if [[ `keystone user-list | grep cinder | wc -l` -eq 0 ]]; then
CINDER_USER=$(get_id keystone user-create --name=cinder \
                                          --pass="$KEYSTONE_CINDER_SERVICE_PASSWORD" \
                                          --tenant_id $SERVICE_TENANT \
                                          --email=cinder@example.com)
keystone user-role-add --tenant_id $SERVICE_TENANT \
                       --user_id $CINDER_USER \
                       --role_id $ADMIN_ROLE
CINDER_SERVICE=$(get_id keystone service-create \
    --name=cinder \
    --type=volume \
    --description="Cinder Service")
keystone endpoint-create \
    --region RegionOne \
    --service_id $CINDER_SERVICE \
    --publicurl "http://$CINDER_HOST:8776/v1/\$(tenant_id)s" \
    --adminurl "http://$CINDER_HOST:8776/v1/\$(tenant_id)s" \
    --internalurl "http://$CINDER_HOST:8776/v1/\$(tenant_id)s"
fi


unset SERVICE_TOKEN
unset SERVICE_ENDPOINT

#---------------------------------------------------
# Create glance user in Mysql
#---------------------------------------------------

# create user
cnt=`mysql_cmd "select * from mysql.user;" | grep $MYSQL_CINDER_USER | wc -l`
if [[ $cnt -eq 0 ]]; then
    mysql_cmd "create user '$MYSQL_CINDER_USER'@'%' identified by '$MYSQL_CINDER_PASSWORD';"
    mysql_cmd "flush privileges;"
fi

# create database
cnt=`mysql_cmd "show databases;" | grep cinder | wc -l`
if [[ $cnt -eq 0 ]]; then
    mysql_cmd "create database cinder CHARACTER SET utf8;"
    mysql_cmd "grant all privileges on cinder.* to '$MYSQL_CINDER_USER'@'%' identified by '$MYSQL_CINDER_PASSWORD';"
    mysql_cmd "grant all privileges on cinder.* to 'root'@'%' identified by '$MYSQL_ROOT_PASSWORD';"
    mysql_cmd "flush privileges;"
fi

#################################################
#
# Change configuration file.
#
#################################################

[[ -d /etc/cinder ]] && rm -rf /etc/cinder/*
mkdir -p /etc/cinder
cp -rf $TOPDIR/cloud/cinder/etc/cinder/* /etc/cinder/

file=/etc/cinder/api-paste.ini
sed -i "s,auth_host = 127.0.0.1,auth_host = $KEYSTONE_HOST,g" $file
sed -i "s,%SERVICE_TENANT_NAME%,$SERVICE_TENANT_NAME,g" $file
sed -i "s,%SERVICE_USER%,cinder,g" $file
sed -i "s,%SERVICE_PASSWORD%,$KEYSTONE_CINDER_SERVICE_PASSWORD,g" $file

file=/etc/cinder/rootwrap.conf
sed -i "s,filters_path=.*,filters_path=/etc/cinder/rootwrap.d,g" $file

file=/etc/cinder/cinder.conf

mkdir -p /opt/stack/data/cinder
rm -rf /etc/cinder/cinder.conf*
cat <<"EOF">$file
[DEFAULT]
rabbit_password = %RABBITMQ_PASSWORD%
rabbit_host = %RABBITMQ_HOST%
state_path = /opt/stack/data/cinder
osapi_volume_extension = cinder.api.openstack.volume.contrib.standard_extensions
root_helper = sudo /usr/local/bin/cinder-rootwrap /etc/cinder/rootwrap.conf
api_paste_config = /etc/cinder/api-paste.ini
sql_connection = mysql://%MYSQL_CINDER_USER%:%MYSQL_CINDER_PASSWORD%@%MYSQL_HOST%/cinder?charset=utf8
iscsi_helper = tgtadm
volume_name_template = volume-%s
volume_group = %VOLUME_GROUP%
verbose = True
auth_strategy = keystone
EOF
sed -i "s,%RABBITMQ_PASSWORD%,$RABBITMQ_PASSWORD,g" $file
sed -i "s,%RABBITMQ_HOST%,$RABBITMQ_HOST,g" $file
sed -i "s,%MYSQL_CINDER_USER%,$MYSQL_CINDER_USER,g" $file
sed -i "s,%MYSQL_CINDER_PASSWORD%,$MYSQL_CINDER_PASSWORD,g" $file
sed -i "s,%MYSQL_HOST%,$MYSQL_HOST,g" $file
sed -i "s,%VOLUME_GROUP%,$VOLUME_GROUP,g" $file

file=/etc/tgt/targets.conf
sed -i "/cinder/g" $file
echo "include /etc/tgt/conf.d/cinder.conf" >> $file
echo "include /opt/stack/data/cinder/volumes/*" >> $file
cp -rf /etc/cinder/cinder.conf /etc/tgt/conf.d/

###########################################################
#
# SYNC the DataBase.
#
############################################################

pvcreate -ff /dev/vdb
vgcreate cinder-volumes /dev/vdb

cinder-manage db sync

############################################################
#
# Create a script to kill all the services with the name.
#
############################################################


cat <<"EOF" > /root/start.sh
#!/bin/bash
mkdir -p /var/log/nova
python /opt/stack/cinder/bin/cinder-api --config-file /etc/cinder/cinder.conf >/var/log/nova/cinder-api.log 2>&1 &
python /opt/stack/cinder/bin/cinder-volume --config-file /etc/cinder/cinder.conf>/var/log/nova/cinder-volume.log 2>&1 &
python /opt/stack/cinder/bin/cinder-scheduler --config-file /etc/cinder/cinder.conf>/var/log/nova/cinder-scheduler.log 2>&1 &
EOF

chmod +x /root/start.sh
/root/start.sh
rm -rf /tmp/pip*
rm -rf /tmp/tmp*

set +o xtrace
