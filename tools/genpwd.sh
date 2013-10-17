#!/bin/bash

TOPDIR=$(cd $(dirname "$0") && pwd)
passwordrc=$TOPDIR/passwordrc

function read_password {
    set +o xtrace
    var=$1; msg=$2; pw=${!var}

    if [ ! $pw ]; then
        pw=`openssl rand -hex 10`
        eval "$var=$pw"
        echo "$var=$pw" >> $passwordrc
    fi
    set -o xtrace
}
[[ -e $passwordrc ]] && rm -rf $passwordrc
read_password RABBIT_PASSWORD
read_password MYSQL_PASSWORD
read_password SWIFT_HASH
read_password SERVICE_TOKEN
read_password ADMIN_PASSWORD
read_password XENAPI_PASSWORD
chmod a+x $passwordrc
cp $passwordrc $TOPDIR/../config/
