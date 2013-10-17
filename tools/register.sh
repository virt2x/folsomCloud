#!/bin/bash
source ../localrc
ADMIN_PASSWORD=$ADMIN_PASSWORD
HOST_IP=10.239.82.174
ADMIN_USER=admin
ADMIN_TENANT=admin
TOKEN=`curl -s -d  "{\"auth\":{\"passwordCredentials\": {\"username\": \"$ADMIN_USER\", \"password\": \"$ADMIN_PASSWORD\"}, \"tenantName\": \"$ADMIN_TENANT\"}}" -H "Content-type: application/json" http://$KEYSTONE_HOST:5000/v2.0/tokens | python -c "import sys; import json; tok = json.loads(sys.stdin.read()); print tok['access']['token']['id'];"`

glance add -A $TOKEN name="$1" is_public=true container_format=ami disk_format=ami  < <(zcat --force "$2")
