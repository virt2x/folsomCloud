#!/bin/bash

# This script is used to build local repo for UBUNTU 11.10
# Put your debs in /tmp/debs, It will ok.

set -e
set -o xtrace
pkgs=${1:-/tmp/debs}
version=`lsb_release -c -s`
mkdir -p /media/sda7/Backup/Ubuntu/Packages
mkdir -p /media/sda7/Backup/Ubuntu/dists/$version/main/binary-amd64
mkdir -p /media/sda7/Backup/Ubuntu/dists/$version/main/binary-i386
cp -rf $pkgs/*.deb /media/sda7/Backup/Ubuntu/Packages >/dev/null
cd /media/sda7/Backup/Ubuntu/
dpkg-scanpackages Packages /dev/null | gzip > dists/$version/main/binary-amd64/Packages.gz
dpkg-scanpackages Packages /dev/null | gzip > dists/$version/main/binary-i386/Packages.gz
sed -i "/media.*sda7/d" /etc/apt/sources.list
echo "deb file:///media/sda7/Backup/Ubuntu/ $version main" >> /etc/apt/sources.list
apt-get update
rm -rfv /root/os/debs >/dev/null
cp -rf /media/sda7 /root/os/debs
