# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (c) 2012 Citrix Systems, Inc.
# Copyright 2010 OpenStack LLC.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Management class for Pool-related functions (join, eject, etc).
"""

import urlparse

from nova.compute import rpcapi as compute_rpcapi
from nova import config
from nova import exception
from nova import flags
from nova.openstack.common import cfg
from nova.openstack.common import jsonutils
from nova.openstack.common import log as logging
from nova.openstack.common import rpc
from nova.virt.xenapi import pool_states
from nova.virt.xenapi import vm_utils

LOG = logging.getLogger(__name__)

xenapi_pool_opts = [
    cfg.BoolOpt('use_join_force',
                default=True,
                help='To use for hosts with different CPUs'),
    ]

CONF = config.CONF
CONF.register_opts(xenapi_pool_opts)


class ResourcePool(object):
    """
    Implements resource pool operations.
    """
    def __init__(self, session, virtapi):
        host_ref = session.get_xenapi_host()
        host_rec = session.call_xenapi('host.get_record', host_ref)
        self._host_name = host_rec['hostname']
        self._host_addr = host_rec['address']
        self._host_uuid = host_rec['uuid']
        self._session = session
        self._virtapi = virtapi
        self.compute_rpcapi = compute_rpcapi.ComputeAPI()

    def _is_hv_pool(self, context, aggregate_id):
        return pool_states.is_hv_pool(
            self._virtapi.aggregate_metadata_get(context, aggregate_id))

    def _get_metadata(self, context, aggregate_id):
        return self._virtapi.aggregate_metadata_get(context, aggregate_id)

    def undo_aggregate_operation(self, context, op, aggregate_id,
                                  host, set_error):
        """Undo aggregate operation when pool error raised"""
        try:
            if set_error:
                metadata = {pool_states.KEY: pool_states.ERROR}
                self._virtapi.aggregate_metadata_add(context, aggregate_id,
                                                     metadata)
            op(context, aggregate_id, host)
        except Exception:
            LOG.exception(_('Aggregate %(aggregate_id)s: unrecoverable state '
                            'during operation on %(host)s') % locals())

    def add_to_aggregate(self, context, aggregate, host, subordinate_info=None):
        """Add a compute host to an aggregate."""
        if not self._is_hv_pool(context, aggregate.id):
            return

        invalid = {pool_states.CHANGING: 'setup in progress',
                   pool_states.DISMISSED: 'aggregate deleted',
                   pool_states.ERROR: 'aggregate in error'}

        if (self._get_metadata(context, aggregate.id)[pool_states.KEY]
                in invalid.keys()):
            raise exception.InvalidAggregateAction(
                    action='add host',
                    aggregate_id=aggregate.id,
                    reason=invalid[self._get_metadata(context,
                            aggregate.id)
                    [pool_states.KEY]])

        if (self._get_metadata(context, aggregate.id)[pool_states.KEY]
                == pool_states.CREATED):
            self._virtapi.aggregate_metadata_add(context, aggregate.id,
                                                 {pool_states.KEY:
                                                      pool_states.CHANGING})
        if len(aggregate.hosts) == 1:
            # this is the first host of the pool -> make it main
            self._init_pool(aggregate.id, aggregate.name)
            # save metadata so that we can find the main again
            metadata = {'main_compute': host,
                        host: self._host_uuid,
                        pool_states.KEY: pool_states.ACTIVE}
            self._virtapi.aggregate_metadata_add(context, aggregate.id,
                                                 metadata)
        else:
            # the pool is already up and running, we need to figure out
            # whether we can serve the request from this host or not.
            main_compute = self._get_metadata(context,
                    aggregate.id)['main_compute']
            if main_compute == CONF.host and main_compute != host:
                # this is the main ->  do a pool-join
                # To this aim, nova compute on the subordinate has to go down.
                # NOTE: it is assumed that ONLY nova compute is running now
                self._join_subordinate(aggregate.id, host,
                                 subordinate_info.get('compute_uuid'),
                                 subordinate_info.get('url'), subordinate_info.get('user'),
                                 subordinate_info.get('passwd'))
                metadata = {host: subordinate_info.get('xenhost_uuid'), }
                self._virtapi.aggregate_metadata_add(context, aggregate.id,
                                                     metadata)
            elif main_compute and main_compute != host:
                # send rpc cast to main, asking to add the following
                # host with specified credentials.
                subordinate_info = self._create_subordinate_info()

                self.compute_rpcapi.add_aggregate_host(
                    context, aggregate, host, main_compute, subordinate_info)

    def remove_from_aggregate(self, context, aggregate, host, subordinate_info=None):
        """Remove a compute host from an aggregate."""
        subordinate_info = subordinate_info or dict()
        if not self._is_hv_pool(context, aggregate.id):
            return

        invalid = {pool_states.CREATED: 'no hosts to remove',
                   pool_states.CHANGING: 'setup in progress',
                   pool_states.DISMISSED: 'aggregate deleted', }
        if (self._get_metadata(context, aggregate.id)[pool_states.KEY]
                in invalid.keys()):
            raise exception.InvalidAggregateAction(
                    action='remove host',
                    aggregate_id=aggregate.id,
                    reason=invalid[self._get_metadata(context,
                            aggregate.id)[pool_states.KEY]])

        main_compute = self._get_metadata(context,
                aggregate.id)['main_compute']
        if main_compute == CONF.host and main_compute != host:
            # this is the main -> instruct it to eject a host from the pool
            host_uuid = self._get_metadata(context, aggregate.id)[host]
            self._eject_subordinate(aggregate.id,
                              subordinate_info.get('compute_uuid'), host_uuid)
            self._virtapi.aggregate_metadata_delete(context, aggregate.id,
                                                    host)
        elif main_compute == host:
            # Remove main from its own pool -> destroy pool only if the
            # main is on its own, otherwise raise fault. Destroying a
            # pool made only by main is fictional
            if len(aggregate.hosts) > 1:
                # NOTE: this could be avoided by doing a main
                # re-election, but this is simpler for now.
                raise exception.InvalidAggregateAction(
                                    aggregate_id=aggregate.id,
                                    action='remove_from_aggregate',
                                    reason=_('Unable to eject %(host)s '
                                             'from the pool; pool not empty')
                                             % locals())
            self._clear_pool(aggregate.id)
            for key in ['main_compute', host]:
                self._virtapi.aggregate_metadata_delete(context, aggregate.id,
                                                        key)
        elif main_compute and main_compute != host:
            # A main exists -> forward pool-eject request to main
            subordinate_info = self._create_subordinate_info()

            self.compute_rpcapi.remove_aggregate_host(
                context, aggregate.id, host, main_compute, subordinate_info)
        else:
            # this shouldn't have happened
            raise exception.AggregateError(aggregate_id=aggregate.id,
                                           action='remove_from_aggregate',
                                           reason=_('Unable to eject %(host)s '
                                           'from the pool; No main found')
                                           % locals())

    def _join_subordinate(self, aggregate_id, host, compute_uuid, url, user, passwd):
        """Joins a subordinate into a XenServer resource pool."""
        try:
            args = {'compute_uuid': compute_uuid,
                    'url': url,
                    'user': user,
                    'password': passwd,
                    'force': jsonutils.dumps(CONF.use_join_force),
                    'main_addr': self._host_addr,
                    'main_user': CONF.xenapi_connection_username,
                    'main_pass': CONF.xenapi_connection_password, }
            self._session.call_plugin('xenhost', 'host_join', args)
        except self._session.XenAPI.Failure as e:
            LOG.error(_("Pool-Join failed: %(e)s") % locals())
            raise exception.AggregateError(aggregate_id=aggregate_id,
                                           action='add_to_aggregate',
                                           reason=_('Unable to join %(host)s '
                                                  'in the pool') % locals())

    def _eject_subordinate(self, aggregate_id, compute_uuid, host_uuid):
        """Eject a subordinate from a XenServer resource pool."""
        try:
            # shutdown nova-compute; if there are other VMs running, e.g.
            # guest instances, the eject will fail. That's a precaution
            # to deal with the fact that the admin should evacuate the host
            # first. The eject wipes out the host completely.
            vm_ref = self._session.call_xenapi('VM.get_by_uuid', compute_uuid)
            self._session.call_xenapi("VM.clean_shutdown", vm_ref)

            host_ref = self._session.call_xenapi('host.get_by_uuid', host_uuid)
            self._session.call_xenapi("pool.eject", host_ref)
        except self._session.XenAPI.Failure as e:
            LOG.error(_("Pool-eject failed: %(e)s") % locals())
            raise exception.AggregateError(aggregate_id=aggregate_id,
                                           action='remove_from_aggregate',
                                           reason=str(e.details))

    def _init_pool(self, aggregate_id, aggregate_name):
        """Set the name label of a XenServer pool."""
        try:
            pool_ref = self._session.call_xenapi("pool.get_all")[0]
            self._session.call_xenapi("pool.set_name_label",
                                      pool_ref, aggregate_name)
        except self._session.XenAPI.Failure as e:
            LOG.error(_("Unable to set up pool: %(e)s.") % locals())
            raise exception.AggregateError(aggregate_id=aggregate_id,
                                           action='add_to_aggregate',
                                           reason=str(e.details))

    def _clear_pool(self, aggregate_id):
        """Clear the name label of a XenServer pool."""
        try:
            pool_ref = self._session.call_xenapi('pool.get_all')[0]
            self._session.call_xenapi('pool.set_name_label', pool_ref, '')
        except self._session.XenAPI.Failure as e:
            LOG.error(_("Pool-set_name_label failed: %(e)s") % locals())
            raise exception.AggregateError(aggregate_id=aggregate_id,
                                           action='remove_from_aggregate',
                                           reason=str(e.details))

    def _create_subordinate_info(self):
        """XenServer specific info needed to join the hypervisor pool"""
        # replace the address from the xenapi connection url
        # because this might be 169.254.0.1, i.e. xenapi
        # NOTE: password in clear is not great, but it'll do for now
        sender_url = swap_xapi_host(
            CONF.xenapi_connection_url, self._host_addr)

        return {
            "url": sender_url,
            "user": CONF.xenapi_connection_username,
            "passwd": CONF.xenapi_connection_password,
            "compute_uuid": vm_utils.get_this_vm_uuid(),
            "xenhost_uuid": self._host_uuid,
        }


def swap_xapi_host(url, host_addr):
    """Replace the XenServer address present in 'url' with 'host_addr'."""
    temp_url = urlparse.urlparse(url)
    _netloc, sep, port = temp_url.netloc.partition(':')
    return url.replace(temp_url.netloc, '%s%s%s' % (host_addr, sep, port))
