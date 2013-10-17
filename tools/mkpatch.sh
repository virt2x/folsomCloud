#!/bin/bash

rm -rfv /tmp/new >/dev/null
mkdir -p /tmp/new

cd /opt/stack

for n in `find . -name "*.py"`
do
    sub_path=`dirname $n`
    mkdir -p /tmp/new/$sub_path
    cp -rf $n /tmp/new/$sub_path
done


for n in `find . -name "*.html"`
do
    sub_path=`dirname $n`
    mkdir -p /tmp/new/$sub_path
    cp -rf $n /tmp/new/$sub_path
done



for n in `find . -name "*.sh"`
do
    sub_path=`dirname $n`
    mkdir -p /tmp/new/$sub_path
    cp -rf $n /tmp/new/$sub_path
done


rm -rfv /tmp/old >/dev/null
mkdir -p /tmp/old

cd /root/os/essex

for n in `find . -name "*.html"`
do
    sub_path=`dirname $n`
    mkdir -p /tmp/old/$sub_path
    cp -rf $n /tmp/old/$sub_path
done


for n in `find . -name "*.py"`
do
    sub_path=`dirname $n`
    mkdir -p /tmp/old/$sub_path
    cp -rf $n /tmp/old/$sub_path
done

for n in `find . -name "*.sh"`
do
    sub_path=`dirname $n`
    mkdir -p /tmp/old/$sub_path
    cp -rf $n /tmp/old/$sub_path
done

for n in `find /tmp/new -name "build"`
do
rm -rfv $n
done

cp -rf /opt/stack/devstack/localrc /tmp/new/devstack/
cd /tmp/
diff -ruN old new >patch
cp /tmp/patch /root/os/

