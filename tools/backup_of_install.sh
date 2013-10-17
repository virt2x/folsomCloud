#!/bin/bash
set -e
set -o xtrace



TOP_DIR=$(cd $(dirname "$0") && pwd)
TEMP=`mktemp`; rm -rfv $TEMP >/dev/null; mkdir -p $TEMP;
DEST=/opt/stack

#---------------------------------------------
# Copy source code
#---------------------------------------------

if [[ ! -d $DEST ]]; then
    mkdir -p $DEST
    cp -rf $TOP_DIR/cloud/* $DEST/
fi

#---------------------------------------------
# Configure new apt sources
#---------------------------------------------

if [[ ! -d /media/sda7 ]]; then
    apt_file=/etc/apt/sources.list
    [[ -e $file ]] && cp -rf $apt_file ${apt_file}"bak"
    mv $TOP_DIR/debs /media/sda7
    version=`lsb_release -c -s`
    echo "deb file:///media/sda7/Backup/Ubuntu/ $version main" > $apt_file
    apt-get update >/dev/null
fi

#---------------------------------------------
# Install packages.
#---------------------------------------------

DEBIAN_FRONTEND=noninteractive \
apt-get --option "Dpkg::Options::=--force-confold" --assume-yes \
install -y --force-yes tgt lvm2 bridge-utils pep8 pylint python-pip \
unzip wget psmisc git-core lsof openssh-server \
vim-nox locate python-virtualenv python-unittest2 \
iputils-ping wget curl tcpdump euca2ools tar python-cmd2 \
gcc python-dateutil  lvm2 open-iscsi open-iscsi-utils \
python-numpy dnsmasq-base dnsmasq-utils kpartx parted \
iputils-arping mysql-client python-mysqldb \
python-xattr python-lxml kvm gawk iptables ebtables \
sqlite3 sudo kvm vlan curl socat \
python-mox python-paste python-migrate python-gflags \
python-greenlet python-libxml2 python-routes python-netaddr \
python-pastedeploy python-eventlet python-cheetah python-carrot \
python-tempita python-sqlalchemy python-suds python-lockfile \
python-m2crypto python-boto python-kombu python-feedparser \
python-iso8601 python-qpid tgt lvm2 iptables sudo \
python-paste python-routes python-netaddr python-pastedeploy \
python-greenlet python-kombu python-eventlet python-sqlalchemy \
python-mysqldb python-pyudev python-qpid dnsmasq-base dnsmasq-utils \
build-essential libxml2 libxml2-dev \
libxslt1.1 libxslt1-dev vlan gnutls-bin \
libgnutls-dev cdbs debhelper libncurses5-dev \
libreadline-dev libavahi-client-dev libparted0-dev \
libdevmapper-dev libudev-dev libpciaccess-dev \
libcap-ng-dev libnl-3-dev libapparmor-dev \
python-all-dev libxen-dev policykit-1 libyajl-dev \
libpcap0.8-dev libnuma-dev radvd libxml2-utils \
libnl-route-3-200 libnl-route-3-dev libnuma1 numactl \
libnuma-dbg libnuma-dev dh-buildinfo expect


[[ -e /usr/include/libxml ]] && rm -rf /usr/include/libxml
ln -s /usr/include/libxml2/libxml /usr/include/libxml
[[ -e /usr/include/netlink ]] && rm -rf /usr/include/netlink
ln -s /usr/include/libnl3/netlink /usr/include/netlink


#---------------------------------------------
# Install libvirt. Complie it.
#---------------------------------------------


if [[ ! -d /opt/libvirt-0.10.2 ]]; then
    old_dir=`pwd`
    cp -rf $TOP_DIR/devstack/files/libvirt-0.10.2.tar.gz /opt/
    cd /opt/
    tar zxf /opt/libvirt-0.10.2.tar.gz
    cd libvirt-0.10.2
    ./configure --prefix=/usr
    make; make install
    cd $old_dir
fi



#---------------------------------------------
# python setup.py develop other modules.
#---------------------------------------------

function source_install()
{
    cd $DEST/$1
    echo $1 >>/tmp/ret
    git checkout master
    python setup.py build
    python setup.py develop
}


#---------------------------------------------
# python-keystoneclient
#---------------------------------------------

pip install $TOP_DIR/pip/prettytable-0.6.1.zip
source_install python-keystoneclient


#---------------------------------------------
# python-keystoneclient
#---------------------------------------------

pip install $TOP_DIR/pip/warlock-0.6.0.tar.gz
pip install $TOP_DIR/pip/jsonschema-0.7.zip
source_install python-glanceclient

#---------------------------------------------
# python-cinderclient
#---------------------------------------------

source_install python-cinderclient


#---------------------------------------------
# python-quantumclient
#---------------------------------------------

pip install $TOP_DIR/pip/pyparsing-1.5.6.zip
pip install $TOP_DIR/pip/cliff-1.3.tar.gz
source_install python-quantumclient


#---------------------------------------------
# python-novaclient
#---------------------------------------------

source_install python-novaclient


#---------------------------------------------
# python-openstackclient
#---------------------------------------------

source_install python-openstackclient


#---------------------------------------------
# Nova
#---------------------------------------------

pip install $TOP_DIR/pip/SQLAlchemy-0.7.9.tar.gz
pip install $TOP_DIR/pip/amqplib-0.6.1.tgz
pip install $TOP_DIR/pip/boto-2.1.1.tar.gz
pip install $TOP_DIR/pip/eventlet-0.9.17.tar.gz
pip install $TOP_DIR/pip/kombu-1.0.4.tar.gz
pip install $TOP_DIR/pip/WebOb-1.0.8.zip
pip install $TOP_DIR/pip/suds-0.4.tar.gz
pip install $TOP_DIR/pip/Babel-0.9.6.tar.gz
pip install $TOP_DIR/pip/Markdown-2.2.1.tar.gz
source_install nova


#---------------------------------------------
# Cinder
#---------------------------------------------

pip install $TOP_DIR/pip/python-daemon-1.5.5.tar.gz
source_install cinder


#---------------------------------------------
# quantum
#---------------------------------------------

source_install quantum



#---------------------------------------------
# Kill process by name
#---------------------------------------------

cp -rf $TOP_DIR/tools/nkill /usr/bin/
chmod +x /usr/bin/nkill


#---------------------------------------------
# configure vim
#---------------------------------------------

cp -rf $TOP_DIR/tools/.vimrc ~/


set +o xtrace
