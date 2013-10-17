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
HOST_IP=10.239.32.27


DEBIAN_FRONTEND=noninteractive \
apt-get --option \
"Dpkg::Options::=--force-confold" --assume-yes \
install -y --force-yes mysql-client

nkill cinder
nkill nova
nkill quantum

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
iscsitarget iscsitarget-dkms open-iscsi build-essential libxml2 libxml2-dev \
libxslt1.1 libxslt1-dev vlan gnutls-bin \
libgnutls-dev cdbs debhelper libncurses5-dev \
libreadline-dev libavahi-client-dev libparted0-dev \
libdevmapper-dev libudev-dev libpciaccess-dev \
libcap-ng-dev libnl-3-dev libapparmor-dev \
python-all-dev libxen-dev policykit-1 libyajl-dev \
libpcap0.8-dev libnuma-dev radvd libxml2-utils \
libnl-route-3-200 libnl-route-3-dev libnuma1 numactl \
libnuma-dbg libnuma-dev dh-buildinfo expect \
make fakeroot dkms openvswitch-switch openvswitch-datapath-dkms \
ebtables iptables iputils-ping iputils-arping sudo python-boto \
python-iso8601 python-routes python-suds python-netaddr \
 python-greenlet python-kombu python-eventlet \
python-sqlalchemy python-mysqldb python-pyudev python-qpid dnsmasq-base \
dnsmasq-utils vlan

[[ -e /usr/include/libxml ]] && rm -rf /usr/include/libxml
ln -s /usr/include/libxml2/libxml /usr/include/libxml
[[ -e /usr/include/netlink ]] && rm -rf /usr/include/netlink
ln -s /usr/include/libnl3/netlink /usr/include/netlink

service ssh restart

if [[ ! -d /opt/libvirt-0.10.2 ]]; then
    old_dir=`pwd`
    now_dir=$(cd $(dirname "$0") && pwd)
    cp -rf $now_dir/tools/libvirt-0.10.2.tar.gz /opt/
    cd /opt/
    tar zxf /opt/libvirt-0.10.2.tar.gz
    cd libvirt-0.10.2
    ./configure --prefix=/usr
    make; make install
    cd $old_dir
fi

#---------------------------------------------------
# Copy source code to DEST Dir
#---------------------------------------------------

[[ ! -d $DEST ]] && mkdir -p $DEST
if [[ ! -d $DEST/nova ]]; then

    cp -rf $TOPDIR/cloud/* /opt/stack/
    #pam, WebOb, PasteDeploy, paste, sqlalchemy, passlib
    pip_install pam-0.1.4.tar.gz
    pip_install WebOb-1.0.8.zip
    pip_install PasteDeploy-1.5.0.tar.gz
    pip_install Paste-1.7.5.1.tar.gz
    pip_install SQLAlchemy-0.7.9.tar.gz
    pip_install passlib-1.6.1.tar.gz
    source_install keystone

    pip_install prettytable-0.6.1.tar.bz2
    source_install python-keystoneclient

    source_install python-swiftclient

    pip_install eventlet-0.9.15.tar.gz
    pip_install netifaces-0.6.tar.gz
    pip_install PasteDeploy-1.3.3.tar.gz    
    pip_install simplejson-2.0.9.tar.gz
    pip_install xattr-0.4.tar.gz
    source_install swift

    source_install swift3

    pip_install boto-2.1.1.tar.gz
    pip_install jsonschema-0.7.zip
    source_install glance

    pip_install warlock-0.7.0.tar.gz
    pip_install jsonpatch-0.10.tar.gz
    pip_install jsonpointer-0.5.tar.gz
    source_install python-glanceclient

    pip_install amqplib-0.6.1.tgz
    pip_install eventlet-0.9.17.tar.gz
    pip_install kombu-1.0.4.tar.gz
    pip_install lockfile-0.8.tar.gz
    pip_install python-daemon-1.5.5.tar.gz
    pip_install PasteDeploy-1.5.0.tar.gz
    pip_install suds-0.4.tar.gz
    pip_install paramiko-1.9.0.tar.gz
    pip_install Babel-0.9.6.tar.gz
    pip_install setuptools-git-0.4.2.tar.gz
    source_install cinder

    source_install python-cinderclient

    pip_install cliff-1.3.tar.gz
    pip_install pyparsing-1.5.6.zip
    pip_install cmd2-0.6.4.tar.gz
    source_install python-quantumclient

    source_install quantum

    pip_install Cheetah-2.4.4.tar.gz
    pip_install Markdown-2.2.1.tar.gz
    source_install nova

    source_install python-novaclient
    source_install python-openstackclient

    pip_install Django-1.4.2.tar.gz
    pip_install django_compressor-1.2.tar.gz
    pip_install django_openstack_auth-1.0.4.tar.gz
    pip_install pytz-2012h.tar.bz2
    pip_install django-appconf-0.5.tar.gz
    source_install horizon        
fi



#---------------------------------------------------
# Create User in Keystone
#---------------------------------------------------
pvcreate -ff /dev/vdb
vgcreate cinder-volumes /dev/vdb

nkill libvirtd
/usr/sbin/libvirtd -d

cnt=`ovs-vsctl show | grep "br-int" | wc -l`
if [[ $cnt -eq 0 ]]; then
    ovs-vsctl add-br br-eth1
    ovs-vsctl add-port br-eth1 eth1
    ovs-vsctl add-br br-int
fi

if [[ ! -d /etc/nova ]] ; then
    scp -pr $NOVA_HOST:/etc/nova /etc/
    scp -pr $CINDER_HOST:/etc/cinder /etc/
    scp -pr $QUANTUM_HOST:/etc/quantum /etc/
    sed -i "s,my_ip=.*,my_ip=$HOST_IP,g" /etc/nova/nova.conf
    sed -i "s,VNCSERVER_PROXYCLIENT_ADDRESS=.*,VNCSERVER_PROXYCLIENT_ADDRESS=$HOST_IP,g" /etc/nova/nova.conf
fi



file=/etc/tgt/targets.conf
sed -i "/cinder/g" $file
echo "include /etc/tgt/conf.d/cinder.conf" >> $file
echo "include /opt/stack/data/cinder/volumes/*" >> $file
cp -rf /etc/cinder/cinder.conf /etc/tgt/conf.d/

mkdir -p $DEST/data/nova/instances/



cat <<"EOF" > /root/start.sh
#!/bin/bash
mkdir -p /var/log/nova

cd /opt/stack/noVNC/
python ./utils/nova-novncproxy --config-file /etc/nova/nova.conf --web . >/var/log/nova/nova-novncproxy.log 2>&1 &

python /opt/stack/nova/bin/nova-xvpvncproxy --config-file /etc/nova/nova.conf >/var/log/nova/nova-xvpvncproxy.log 2>&1 &

nohup python /opt/stack/quantum/bin/quantum-l3-agent --config-file /etc/quantum/quantum.conf --config-file=/etc/quantum/l3_agent.ini > /var/log/nova/quantum-l3.log 2>&1 &

nohup python /opt/stack/nova/bin/nova-compute --config-file=/etc/nova/nova.conf >/var/log/nova/nova-compute.log 2>&1 &

python /opt/stack/cinder/bin/cinder-volume --config-file /etc/cinder/cinder.conf>/var/log/nova/cinder-volume.log 2>&1 &
EOF

chmod +x /root/start.sh
/root/start.sh
rm -rf /tmp/pip*
rm -rf /tmp/tmp*

set +o xtrace
