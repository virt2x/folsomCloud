#!/bin/bash
set -e
set -o xtrace



TOP_DIR=$(cd $(dirname "$0") && pwd)
TEMP=`mktemp`; rm -rfv $TEMP >/dev/null; mkdir -p $TEMP;
DEST=/opt/stack
source $TOP_DIR/tools/function

#---------------------------------------------
# Check for apt.
#---------------------------------------------
apt-get update
DEBIAN_FRONTEND=noninteractive \
apt-get --option \
"Dpkg::Options::=--force-confold" --assume-yes \
install -y --force-yes openssh-server 


#---------------------------------------------
# Configure base system.
#---------------------------------------------

mkdir -p /root/.add
cp -rf $TOP_DIR/tools/addmac.sh /root/.add/

#---------------------------------------------
# Change rc.local
#---------------------------------------------


sed -i "/exit/d" /etc/rc.local
sed -i "/addmac/d" /etc/rc.local
echo "/root/.add/addmac.sh" >> /etc/rc.local
echo "exit 0" >> /etc/rc.local

#---------------------------------------------
# Kill process by Name
#---------------------------------------------

cp -rf $TOP_DIR/tools/nkill /usr/bin/
chmod +x /usr/bin/

#---------------------------------------------
# Copy pip package build shell.
#---------------------------------------------

cp -rf $TOP_DIR/tools/ch.sh /tmp/
setup_iptables
set +o xtrace
