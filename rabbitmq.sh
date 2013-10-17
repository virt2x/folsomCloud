#!/bin/bash

set -e
set +o xtrace
#---------------------------------------------
# Set variable
#---------------------------------------------

TOP_DIR=$(cd $(dirname "$0") && pwd)
TEMP=`mktemp`;
rm -rfv $TEMP >/dev/null
mkdir -p $TEMP;
source $TOP_DIR/localrc
#---------------------------------------------
# source localrc
#---------------------------------------------

rabbitmqctl change_password guest $RABBITMQ_PASSWORD

set -o xtrace
