# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# Copyright 2011 Justin Santa Barbara
# All Rights Reserved.
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

"""Handles all processes relating to instances (guest vms).

The :py:class:`ComputeManager` class is a :py:class:`nova.manager.Manager` that
handles RPC calls relating to creating instances.  It is responsible for
building a disk image, launching it via the underlying virtualization driver,
responding to calls to check its state, attaching persistent storage, and
terminating it.

**Related Flags**

:instances_path:  Where instances are kept on disk
:base_dir_name:  Where cached images are stored under instances_path
:compute_driver:  Name of class that is used to handle virtualization, loaded
                  by :func:`nova.openstack.common.importutils.import_object`

"""

import contextlib
import functools
import socket
import sys
import time
import traceback
import uuid

from eventlet import greenthread

from nova import block_device
from nova import compute
from nova.compute import instance_types
from nova.compute import power_state
from nova.compute import resource_tracker
from nova.compute import rpcapi as compute_rpcapi
from nova.compute import task_states
from nova.compute import utils as compute_utils
from nova.compute import vm_states
from nova import config
import nova.context
from nova import exception
from nova import flags
from nova.image import glance
from nova import manager
from nova import network
from nova.network import model as network_model
from nova import notifications
from nova.openstack.common import cfg
from nova.openstack.common import excutils
from nova.openstack.common import importutils
from nova.openstack.common import jsonutils
from nova.openstack.common import lockutils
from nova.openstack.common import log as logging
from nova.openstack.common.notifier import api as notifier
from nova.openstack.common import rpc
from nova.openstack.common import timeutils
from nova import quota
from nova.scheduler import rpcapi as scheduler_rpcapi
from nova import utils
from nova.virt import driver
from nova.virt import virtapi
from nova import volume


compute_opts = [
    cfg.StrOpt('instances_path',
               default='$state_path/instances',
               help='where instances are stored on disk'),
    cfg.StrOpt('base_dir_name',
               default='_base',
               help="Where cached images are stored under $instances_path."
                    "This is NOT the full path - just a folder name."
                    "For per-compute-host cached images, set to _base_$my_ip"),
    cfg.StrOpt('console_host',
               default=socket.getfqdn(),
               help='Console proxy host to use to connect '
                    'to instances on this host.'),
    cfg.IntOpt('live_migration_retry_count',
               default=30,
               help="Number of 1 second retries needed in live_migration"),
    cfg.IntOpt("reboot_timeout",
               default=0,
               help="Automatically hard reboot an instance if it has been "
                    "stuck in a rebooting state longer than N seconds. "
                    "Set to 0 to disable."),
    cfg.IntOpt("instance_build_timeout",
               default=0,
               help="Amount of time in seconds an instance can be in BUILD "
                    "before going into ERROR status."
                    "Set to 0 to disable."),
    cfg.IntOpt("rescue_timeout",
               default=0,
               help="Automatically unrescue an instance after N seconds. "
                    "Set to 0 to disable."),
    cfg.IntOpt("resize_confirm_window",
               default=0,
               help="Automatically confirm resizes after N seconds. "
                    "Set to 0 to disable."),
    cfg.IntOpt('host_state_interval',
               default=120,
               help='Interval in seconds for querying the host status'),
    cfg.IntOpt("running_deleted_instance_timeout",
               default=0,
               help="Number of seconds after being deleted when a running "
                    "instance should be considered eligible for cleanup."),
    cfg.IntOpt("running_deleted_instance_poll_interval",
               default=30,
               help="Number of periodic scheduler ticks to wait between "
                    "runs of the cleanup task."),
    cfg.StrOpt("running_deleted_instance_action",
               default="log",
               help="Action to take if a running deleted instance is detected."
                    "Valid options are 'noop', 'log' and 'reap'. "
                    "Set to 'noop' to disable."),
    cfg.IntOpt("image_cache_manager_interval",
               default=40,
               help="Number of periodic scheduler ticks to wait between "
                    "runs of the image cache manager."),
    cfg.IntOpt("heal_instance_info_cache_interval",
               default=60,
               help="Number of seconds between instance info_cache self "
                        "healing updates"),
    cfg.BoolOpt('instance_usage_audit',
               default=False,
               help="Generate periodic compute.instance.exists notifications"),
    ]

CONF = config.CONF
CONF.register_opts(compute_opts)

QUOTAS = quota.QUOTAS

LOG = logging.getLogger(__name__)


def publisher_id(host=None):
    return notifier.publisher_id("compute", host)


def reverts_task_state(function):
    """Decorator to revert task_state on failure"""

    @functools.wraps(function)
    def decorated_function(self, context, *args, **kwargs):
        try:
            return function(self, context, *args, **kwargs)
        except exception.UnexpectedTaskStateError:
            LOG.exception(_("Possibly task preempted."))
            # Note(maoy): unexpected task state means the current
            # task is preempted. Do not clear task state in this
            # case.
            raise
        except Exception:
            with excutils.save_and_reraise_exception():
                try:
                    self._instance_update(context,
                                          kwargs['instance']['uuid'],
                                          task_state=None)
                except Exception:
                    pass

    return decorated_function


def wrap_instance_fault(function):
    """Wraps a method to catch exceptions related to instances.

    This decorator wraps a method to catch any exceptions having to do with
    an instance that may get thrown. It then logs an instance fault in the db.
    """

    @functools.wraps(function)
    def decorated_function(self, context, *args, **kwargs):
        try:
            return function(self, context, *args, **kwargs)
        except exception.InstanceNotFound:
            raise
        except Exception, e:
            # NOTE(gtt): If argument 'instance' is in args rather than kwargs,
            # we will get a KeyError exception which will cover up the real
            # exception. So, we update kwargs with the values from args first.
            # then, we can get 'instance' from kwargs easily.
            kwargs.update(dict(zip(function.func_code.co_varnames[2:], args)))

            with excutils.save_and_reraise_exception():
                compute_utils.add_instance_fault_from_exc(context,
                        kwargs['instance']['uuid'], e, sys.exc_info())

    return decorated_function


def _get_image_meta(context, image_ref):
    image_service, image_id = glance.get_remote_image_service(context,
                                                              image_ref)
    return image_service.show(context, image_id)


class ComputeVirtAPI(virtapi.VirtAPI):
    def __init__(self, compute):
        super(ComputeVirtAPI, self).__init__()
        self._compute = compute

    def instance_update(self, context, instance_uuid, updates):
        return self._compute.db.instance_update_and_get_original(context,
                                                                 instance_uuid,
                                                                 updates)

    def instance_get_by_uuid(self, context, instance_uuid):
        return self._compute.db.instance_get_by_uuid(context, instance_uuid)

    def instance_get_all_by_host(self, context, host):
        return self._compute.db.instance_get_all_by_host(context, host)

    def aggregate_get_by_host(self, context, host, key=None):
        return self._compute.db.aggregate_get_by_host(context, host, key=key)

    def aggregate_metadata_get(self, context, aggregate_id):
        return self._compute.db.aggregate_metadata_get(context, aggregate_id)

    def aggregate_metadata_add(self, context, aggregate_id, metadata,
                               set_delete=False):
        return self._compute.db.aggregate_metadata_add(context, aggregate_id,
                                                       metadata,
                                                       set_delete=set_delete)

    def aggregate_metadata_delete(self, context, aggregate_id, key):
        return self._compute.db.aggregate_metadata_delete(context,
                                                          aggregate_id, key)

    def security_group_get_by_instance(self, context, instance_uuid):
        return self._compute.db.security_group_get_by_instance(context,
                                                               instance_uuid)

    def security_group_rule_get_by_security_group(self, context,
                                                  security_group_id):
        return self._compute.db.security_group_rule_get_by_security_group(
            context, security_group_id)

    def provider_fw_rule_get_all(self, context):
        return self._compute.db.provider_fw_rule_get_all(context)

    def agent_build_get_by_triple(self, context, hypervisor, os, architecture):
        return self._compute.db.agent_build_get_by_triple(context,
                                                          hypervisor,
                                                          os,
                                                          architecture)


class ComputeManager(manager.SchedulerDependentManager):
    """Manages the running instances from creation to destruction."""

    RPC_API_VERSION = '2.16'

    def __init__(self, compute_driver=None, *args, **kwargs):
        """Load configuration options and connect to the hypervisor."""
        # TODO(vish): sync driver creation logic with the rest of the system
        #             and re-document the module docstring
        if not compute_driver:
            compute_driver = CONF.compute_driver

        if not compute_driver:
            LOG.error(_("Compute driver option required, but not specified"))
            sys.exit(1)

        self.virtapi = ComputeVirtAPI(self)

        LOG.info(_("Loading compute driver '%s'") % compute_driver)
        try:
            self.driver = utils.check_isinstance(
                    importutils.import_object_ns('nova.virt', compute_driver,
                                                 self.virtapi),
                    driver.ComputeDriver)
        except ImportError as e:
            LOG.error(_("Unable to load the virtualization driver: %s") % (e))
            sys.exit(1)

        self.network_api = network.API()
        self.volume_api = volume.API()
        self.network_manager = importutils.import_object(
            CONF.network_manager, host=kwargs.get('host', None))
        self._last_host_check = 0
        self._last_bw_usage_poll = 0
        self._last_info_cache_heal = 0
        self.compute_api = compute.API()
        self.compute_rpcapi = compute_rpcapi.ComputeAPI()
        self.scheduler_rpcapi = scheduler_rpcapi.SchedulerAPI()

        super(ComputeManager, self).__init__(service_name="compute",
                                             *args, **kwargs)

        self._resource_tracker_dict = {}

    def _get_resource_tracker(self, nodename):
        rt = self._resource_tracker_dict.get(nodename)
        if not rt:
            rt = resource_tracker.ResourceTracker(self.host,
                                                  self.driver,
                                                  nodename)
            self._resource_tracker_dict[nodename] = rt
        return rt

    def _instance_update(self, context, instance_uuid, **kwargs):
        """Update an instance in the database using kwargs as value."""

        (old_ref, instance_ref) = self.db.instance_update_and_get_original(
                context, instance_uuid, kwargs)
        rt = self._get_resource_tracker(instance_ref.get('node'))
        rt.update_usage(context, instance_ref)
        notifications.send_update(context, old_ref, instance_ref)

        return instance_ref

    def _set_instance_error_state(self, context, instance_uuid):
        try:
            self._instance_update(context, instance_uuid,
                                  vm_state=vm_states.ERROR)
        except exception.InstanceNotFound:
            LOG.debug(_('Instance has been destroyed from under us while '
                        'trying to set it to ERROR'),
                      instance_uuid=instance_uuid)

    def init_host(self):
        """Initialization for a standalone compute service."""
        self.driver.init_host(host=self.host)
        context = nova.context.get_admin_context()
        instances = self.db.instance_get_all_by_host(context, self.host)

        if CONF.defer_iptables_apply:
            self.driver.filter_defer_apply_on()

        try:
            for count, instance in enumerate(instances):
                db_state = instance['power_state']
                drv_state = self._get_power_state(context, instance)
                closing_vm_states = (vm_states.DELETED,
                                     vm_states.SOFT_DELETED)

                # instance was supposed to shut down - don't attempt
                # recovery in any case
                if instance['vm_state'] in closing_vm_states:
                    continue

                expect_running = (db_state == power_state.RUNNING and
                                  drv_state != db_state)

                LOG.debug(_('Current state is %(drv_state)s, state in DB is '
                            '%(db_state)s.'), locals(), instance=instance)

                net_info = compute_utils.get_nw_info_for_instance(instance)

                # We're calling plug_vifs to ensure bridge and iptables
                # filters are present, calling it once is enough.
                if count == 0:
                    legacy_net_info = self._legacy_nw_info(net_info)
                    self.driver.plug_vifs(instance, legacy_net_info)

                if ((expect_running and CONF.resume_guests_state_on_host_boot)
                     or CONF.start_guests_on_host_boot):
                    LOG.info(
                           _('Rebooting instance after nova-compute restart.'),
                           locals(), instance=instance)

                    block_device_info = \
                        self._get_instance_volume_block_device_info(
                            context, instance['uuid'])

                    try:
                        self.driver.resume_state_on_host_boot(
                                context,
                                instance,
                                self._legacy_nw_info(net_info),
                                block_device_info)
                    except NotImplementedError:
                        LOG.warning(_('Hypervisor driver does not support '
                                      'resume guests'), instance=instance)

                elif drv_state == power_state.RUNNING:
                    # VMWareAPI drivers will raise an exception
                    try:
                        self.driver.ensure_filtering_rules_for_instance(
                                               instance,
                                               self._legacy_nw_info(net_info))
                    except NotImplementedError:
                        LOG.warning(_('Hypervisor driver does not support '
                                      'firewall rules'), instance=instance)

        finally:
            if CONF.defer_iptables_apply:
                self.driver.filter_defer_apply_off()

        self._report_driver_status(context)
        self.publish_service_capabilities(context)

    def pre_start_hook(self):
        """After the service is initialized, but before we fully bring
        the service up by listening on RPC queues, make sure to update
        our available resources.
        """
        self.update_available_resource(nova.context.get_admin_context())

    def _get_power_state(self, context, instance):
        """Retrieve the power state for the given instance."""
        LOG.debug(_('Checking state'), instance=instance)
        try:
            return self.driver.get_info(instance)["state"]
        except exception.NotFound:
            return power_state.NOSTATE

    def get_console_topic(self, context):
        """Retrieves the console host for a project on this host.

        Currently this is just set in the flags for each compute host.

        """
        #TODO(mdragon): perhaps make this variable by console_type?
        return rpc.queue_get_for(context,
                                 CONF.console_topic,
                                 CONF.console_host)

    def get_console_pool_info(self, context, console_type):
        return self.driver.get_console_pool_info(console_type)

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    def refresh_security_group_rules(self, context, security_group_id):
        """Tell the virtualization driver to refresh security group rules.

        Passes straight through to the virtualization driver.

        """
        return self.driver.refresh_security_group_rules(security_group_id)

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    def refresh_security_group_members(self, context, security_group_id):
        """Tell the virtualization driver to refresh security group members.

        Passes straight through to the virtualization driver.

        """
        return self.driver.refresh_security_group_members(security_group_id)

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    def refresh_instance_security_rules(self, context, instance):
        """Tell the virtualization driver to refresh security rules for
        an instance.

        Passes straight through to the virtualization driver.

        """
        return self.driver.refresh_instance_security_rules(instance)

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    def refresh_provider_fw_rules(self, context):
        """This call passes straight through to the virtualization driver."""
        return self.driver.refresh_provider_fw_rules()

    def _get_instance_nw_info(self, context, instance):
        """Get a list of dictionaries of network data of an instance."""
        # get the network info from network
        network_info = self.network_api.get_instance_nw_info(context,
                                                             instance)
        return network_info

    def _legacy_nw_info(self, network_info):
        """Converts the model nw_info object to legacy style"""
        if self.driver.legacy_nwinfo():
            network_info = network_info.legacy()
        return network_info

    def _setup_block_device_mapping(self, context, instance):
        """setup volumes for block device mapping"""
        block_device_mapping = []
        swap = None
        ephemerals = []
        for bdm in self.db.block_device_mapping_get_all_by_instance(
            context, instance['uuid']):
            LOG.debug(_('Setting up bdm %s'), bdm, instance=instance)

            if bdm['no_device']:
                continue
            if bdm['virtual_name']:
                virtual_name = bdm['virtual_name']
                device_name = bdm['device_name']
                assert block_device.is_swap_or_ephemeral(virtual_name)
                if virtual_name == 'swap':
                    swap = {'device_name': device_name,
                            'swap_size': bdm['volume_size']}
                elif block_device.is_ephemeral(virtual_name):
                    eph = {'num': block_device.ephemeral_num(virtual_name),
                           'virtual_name': virtual_name,
                           'device_name': device_name,
                           'size': bdm['volume_size']}
                    ephemerals.append(eph)
                continue

            if ((bdm['snapshot_id'] is not None) and
                (bdm['volume_id'] is None)):
                # TODO(yamahata): default name and description
                snapshot = self.volume_api.get_snapshot(context,
                                                        bdm['snapshot_id'])
                vol = self.volume_api.create(context, bdm['volume_size'],
                                             '', '', snapshot)
                # TODO(yamahata): creating volume simultaneously
                #                 reduces creation time?
                # TODO(yamahata): eliminate dumb polling
                while True:
                    volume = self.volume_api.get(context, vol['id'])
                    if volume['status'] != 'creating':
                        break
                    greenthread.sleep(1)
                self.db.block_device_mapping_update(
                    context, bdm['id'], {'volume_id': vol['id']})
                bdm['volume_id'] = vol['id']

            if bdm['volume_id'] is not None:
                volume = self.volume_api.get(context, bdm['volume_id'])
                self.volume_api.check_attach(context, volume)
                cinfo = self._attach_volume_boot(context,
                                                 instance,
                                                 volume,
                                                 bdm['device_name'])
                self.db.block_device_mapping_update(
                        context, bdm['id'],
                        {'connection_info': jsonutils.dumps(cinfo)})
                bdmap = {'connection_info': cinfo,
                         'mount_device': bdm['device_name'],
                         'delete_on_termination': bdm['delete_on_termination']}
                block_device_mapping.append(bdmap)

        return {
            'root_device_name': instance['root_device_name'],
            'swap': swap,
            'ephemerals': ephemerals,
            'block_device_mapping': block_device_mapping
        }

    def _run_instance(self, context, request_spec,
                      filter_properties, requested_networks, injected_files,
                      admin_password, is_first_time, instance):
        """Launch a new instance with specified options."""
        context = context.elevated()

        try:
            self._check_instance_not_already_created(context, instance)
            image_meta = self._check_image_size(context, instance)
            extra_usage_info = {"image_name": image_meta['name']}
            self._start_building(context, instance)
            self._notify_about_instance_usage(
                    context, instance, "create.start",
                    extra_usage_info=extra_usage_info)
            network_info = None
            rt = self._get_resource_tracker(instance.get('node'))
            try:
                limits = filter_properties.get('limits', {})
                with rt.instance_claim(context, instance, limits):

                    network_info = self._allocate_network(context, instance,
                            requested_networks)
                    block_device_info = self._prep_block_device(context,
                            instance)
                    instance = self._spawn(context, instance, image_meta,
                                           network_info, block_device_info,
                                           injected_files, admin_password)

            except exception.InstanceNotFound:
                # the instance got deleted during the spawn
                try:
                    self._deallocate_network(context, instance)
                except Exception:
                    msg = _('Failed to dealloc network for deleted instance')
                    LOG.exception(msg, instance=instance)
                raise
            except Exception:
                # try to re-schedule instance:
                self._reschedule_or_reraise(context, instance,
                        requested_networks, admin_password, injected_files,
                        is_first_time, request_spec, filter_properties)
            else:
                # Spawn success:
                if (is_first_time and not instance['access_ip_v4']
                                  and not instance['access_ip_v6']):
                    self._update_access_ip(context, instance, network_info)

                self._notify_about_instance_usage(context, instance,
                        "create.end", network_info=network_info,
                        extra_usage_info=extra_usage_info)
        except Exception:
            with excutils.save_and_reraise_exception():
                self._set_instance_error_state(context, instance['uuid'])

    def _log_original_error(self, exc_info, instance_uuid):
        type_, value, tb = exc_info
        LOG.error(_('Error: %s') %
                  traceback.format_exception(type_, value, tb),
                  instance_uuid=instance_uuid)

    def _reschedule_or_reraise(self, context, instance, requested_networks,
                               admin_password, injected_files, is_first_time,
                               request_spec, filter_properties):
        """Try to re-schedule the build or re-raise the original build error to
        error out the instance.
        """
        exc_info = sys.exc_info()
        instance_uuid = instance['uuid']
        rescheduled = False

        try:
            self._deallocate_network(context, instance)
        except Exception:
            # do not attempt retry if network de-allocation failed:
            self._log_original_error(exc_info, instance_uuid)
            raise

        try:
            method_args = (request_spec, admin_password, injected_files,
                    requested_networks, is_first_time, filter_properties)
            task_state = task_states.SCHEDULING

            rescheduled = self._reschedule(context, request_spec,
                    instance['uuid'], filter_properties,
                    self.scheduler_rpcapi.run_instance, method_args,
                    task_state)

        except Exception:
            rescheduled = False
            LOG.exception(_("Error trying to reschedule"),
                          instance_uuid=instance_uuid)

        if rescheduled:
            # log the original build error
            self._log_original_error(exc_info, instance_uuid)
        else:
            # not re-scheduling
            raise exc_info[0], exc_info[1], exc_info[2]

    def _reschedule(self, context, request_spec, filter_properties,
            instance_uuid, scheduler_method, method_args, task_state):
        """Attempt to re-schedule a compute operation."""

        retry = filter_properties.get('retry', None)
        if not retry:
            # no retry information, do not reschedule.
            LOG.debug(_("Retry info not present, will not reschedule"),
                      instance_uuid=instance_uuid)
            return

        if not request_spec:
            LOG.debug(_("No request spec, will not reschedule"),
                      instance_uuid=instance_uuid)
            return

        request_spec['instance_uuids'] = [instance_uuid]

        LOG.debug(_("Re-scheduling %(method)s: attempt %(num)d") %
                {'method': scheduler_method.func_name,
                 'num': retry['num_attempts']}, instance_uuid=instance_uuid)

        # reset the task state:
        self._instance_update(context, instance_uuid, task_state=task_state)

        scheduler_method(context, *method_args)
        return True

    @manager.periodic_task
    def _check_instance_build_time(self, context):
        """Ensure that instances are not stuck in build."""
        timeout = CONF.instance_build_timeout
        if timeout == 0:
            return

        filters = {'vm_state': vm_states.BUILDING}
        building_insts = self.db.instance_get_all_by_filters(context, filters)

        for instance in building_insts:
            if timeutils.is_older_than(instance['created_at'], timeout):
                self._set_instance_error_state(context, instance['uuid'])
                LOG.warn(_("Instance build timed out. Set to error state."),
                         instance=instance)

    def _update_access_ip(self, context, instance, nw_info):
        """Update the access ip values for a given instance.

        If CONF.default_access_ip_network_name is set, this method will
        grab the corresponding network and set the access ip values
        accordingly. Note that when there are multiple ips to choose from,
        an arbitrary one will be chosen.
        """

        network_name = CONF.default_access_ip_network_name
        if not network_name:
            return

        update_info = {}
        for vif in nw_info:
            if vif['network']['label'] == network_name:
                for ip in vif.fixed_ips():
                    if ip['version'] == 4:
                        update_info['access_ip_v4'] = ip['address']
                    if ip['version'] == 6:
                        update_info['access_ip_v6'] = ip['address']
        if update_info:
            self.db.instance_update(context, instance.uuid, update_info)
            notifications.send_update(context, instance, instance)

    def _check_instance_not_already_created(self, context, instance):
        """Ensure an instance with the same name is not already present."""
        if self.driver.instance_exists(instance['name']):
            _msg = _("Instance has already been created")
            raise exception.Invalid(_msg)

    def _check_image_size(self, context, instance):
        """Ensure image is smaller than the maximum size allowed by the
        instance_type.

        The image stored in Glance is potentially compressed, so we use two
        checks to ensure that the size isn't exceeded:

            1) This one - checks compressed size, this a quick check to
               eliminate any images which are obviously too large

            2) Check uncompressed size in nova.virt.xenapi.vm_utils. This
               is a slower check since it requires uncompressing the entire
               image, but is accurate because it reflects the image's
               actual size.
        """
        image_meta = _get_image_meta(context, instance['image_ref'])

        try:
            size_bytes = image_meta['size']
        except KeyError:
            # Size is not a required field in the image service (yet), so
            # we are unable to rely on it being there even though it's in
            # glance.

            # TODO(jk0): Should size be required in the image service?
            return image_meta

        instance_type_id = instance['instance_type_id']
        instance_type = instance_types.get_instance_type(instance_type_id)
        allowed_size_gb = instance_type['root_gb']

        # NOTE(johannes): root_gb is allowed to be 0 for legacy reasons
        # since libvirt interpreted the value differently than other
        # drivers. A value of 0 means don't check size.
        if not allowed_size_gb:
            return image_meta

        allowed_size_bytes = allowed_size_gb * 1024 * 1024 * 1024

        image_id = image_meta['id']
        LOG.debug(_("image_id=%(image_id)s, image_size_bytes="
                    "%(size_bytes)d, allowed_size_bytes="
                    "%(allowed_size_bytes)d") % locals(),
                  instance=instance)

        if size_bytes > allowed_size_bytes:
            LOG.info(_("Image '%(image_id)s' size %(size_bytes)d exceeded"
                       " instance_type allowed size "
                       "%(allowed_size_bytes)d")
                       % locals(), instance=instance)
            raise exception.ImageTooLarge()

        return image_meta

    def _start_building(self, context, instance):
        """Save the host and launched_on fields and log appropriately."""
        LOG.audit(_('Starting instance...'), context=context,
                  instance=instance)
        self._instance_update(context, instance['uuid'],
                              vm_state=vm_states.BUILDING,
                              task_state=None,
                              expected_task_state=(task_states.SCHEDULING,
                                                   None))

    def _allocate_network(self, context, instance, requested_networks):
        """Allocate networks for an instance and return the network info"""
        self._instance_update(context, instance['uuid'],
                              vm_state=vm_states.BUILDING,
                              task_state=task_states.NETWORKING,
                              expected_task_state=None)
        is_vpn = instance['image_ref'] == str(CONF.vpn_image_id)
        try:
            # allocate and get network info
            network_info = self.network_api.allocate_for_instance(
                                context, instance, vpn=is_vpn,
                                requested_networks=requested_networks)
        except Exception:
            LOG.exception(_('Instance failed network setup'),
                          instance=instance)
            raise

        LOG.debug(_('Instance network_info: |%s|'), network_info,
                  instance=instance)

        return network_info

    def _prep_block_device(self, context, instance):
        """Set up the block device for an instance with error logging"""
        self._instance_update(context, instance['uuid'],
                              vm_state=vm_states.BUILDING,
                              task_state=task_states.BLOCK_DEVICE_MAPPING)
        try:
            return self._setup_block_device_mapping(context, instance)
        except Exception:
            LOG.exception(_('Instance failed block device setup'),
                          instance=instance)
            raise

    def _spawn(self, context, instance, image_meta, network_info,
               block_device_info, injected_files, admin_password):
        """Spawn an instance with error logging and update its power state"""
        self._instance_update(context, instance['uuid'],
                              vm_state=vm_states.BUILDING,
                              task_state=task_states.SPAWNING,
                              expected_task_state=task_states.
                                  BLOCK_DEVICE_MAPPING)
        try:
            self.driver.spawn(context, instance, image_meta,
                              injected_files, admin_password,
                              self._legacy_nw_info(network_info),
                              block_device_info)
        except Exception:
            LOG.exception(_('Instance failed to spawn'), instance=instance)
            raise

        current_power_state = self._get_power_state(context, instance)
        return self._instance_update(context, instance['uuid'],
                                     power_state=current_power_state,
                                     vm_state=vm_states.ACTIVE,
                                     task_state=None,
                                     expected_task_state=task_states.SPAWNING,
                                     launched_at=timeutils.utcnow())

    def _notify_about_instance_usage(self, context, instance, event_suffix,
                                     network_info=None, system_metadata=None,
                                     extra_usage_info=None):
        # NOTE(sirp): The only thing this wrapper function does extra is handle
        # the passing in of `self.host`. Ordinarily this will just be
        # CONF.host`, but `Manager`'s gets a chance to override this in its
        # `__init__`.
        compute_utils.notify_about_instance_usage(
                context, instance, event_suffix, network_info=network_info,
                system_metadata=system_metadata,
                extra_usage_info=extra_usage_info, host=self.host)

    def _deallocate_network(self, context, instance):
        LOG.debug(_('Deallocating network for instance'), instance=instance)
        self.network_api.deallocate_for_instance(context, instance)

    def _get_volume_bdms(self, bdms):
        """Return only bdms that have a volume_id"""
        return [bdm for bdm in bdms if bdm['volume_id']]

    # NOTE(danms): Legacy interface for digging up volumes in the database
    def _get_instance_volume_bdms(self, context, instance_uuid):
        return self._get_volume_bdms(
            self.db.block_device_mapping_get_all_by_instance(context,
                                                             instance_uuid))

    def _get_instance_volume_bdm(self, context, instance_uuid, volume_id):
        bdms = self._get_instance_volume_bdms(context, instance_uuid)
        for bdm in bdms:
            # NOTE(vish): Comparing as strings because the os_api doesn't
            #             convert to integer and we may wish to support uuids
            #             in the future.
            if str(bdm['volume_id']) == str(volume_id):
                return bdm

    # NOTE(danms): This is a transitional interface until all the callers
    # can provide their own bdms
    def _get_instance_volume_block_device_info(self, context, instance_uuid,
                                               bdms=None):
        if bdms is None:
            bdms = self._get_instance_volume_bdms(context, instance_uuid)
        return self._get_volume_block_device_info(bdms)

    def _get_volume_block_device_info(self, bdms):
        block_device_mapping = []
        for bdm in bdms:
            try:
                cinfo = jsonutils.loads(bdm['connection_info'])
                if cinfo and 'serial' not in cinfo:
                    cinfo['serial'] = bdm['volume_id']
                bdmap = {'connection_info': cinfo,
                         'mount_device': bdm['device_name'],
                         'delete_on_termination': bdm['delete_on_termination']}
                block_device_mapping.append(bdmap)
            except TypeError:
                # if the block_device_mapping has no value in connection_info
                # (returned as None), don't include in the mapping
                pass
        # NOTE(vish): The mapping is passed in so the driver can disconnect
        #             from remote volumes if necessary
        return {'block_device_mapping': block_device_mapping}

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    @reverts_task_state
    @wrap_instance_fault
    def run_instance(self, context, instance, request_spec=None,
                     filter_properties=None, requested_networks=None,
                     injected_files=None, admin_password=None,
                     is_first_time=False):

        if filter_properties is None:
            filter_properties = {}
        if injected_files is None:
            injected_files = []

        @lockutils.synchronized(instance['uuid'], 'nova-')
        def do_run_instance():
            self._run_instance(context, request_spec,
                    filter_properties, requested_networks, injected_files,
                    admin_password, is_first_time, instance)
        do_run_instance()

    def _shutdown_instance(self, context, instance, bdms):
        """Shutdown an instance on this host."""
        context = context.elevated()
        LOG.audit(_('%(action_str)s instance') % {'action_str': 'Terminating'},
                  context=context, instance=instance)

        self._notify_about_instance_usage(context, instance, "shutdown.start")

        # get network info before tearing down
        try:
            network_info = self._get_instance_nw_info(context, instance)
        except exception.NetworkNotFound:
            network_info = network_model.NetworkInfo()

        # tear down allocated network structure
        self._deallocate_network(context, instance)

        # NOTE(vish) get bdms before destroying the instance
        vol_bdms = self._get_volume_bdms(bdms)
        block_device_info = self._get_instance_volume_block_device_info(
            context, instance['uuid'], bdms=bdms)
        self.driver.destroy(instance, self._legacy_nw_info(network_info),
                            block_device_info)
        for bdm in vol_bdms:
            try:
                # NOTE(vish): actual driver detach done in driver.destroy, so
                #             just tell nova-volume that we are done with it.
                volume = self.volume_api.get(context, bdm['volume_id'])
                connector = self.driver.get_volume_connector(instance)
                self.volume_api.terminate_connection(context,
                                                     volume,
                                                     connector)
                self.volume_api.detach(context, volume)
            except exception.DiskNotFound as exc:
                LOG.warn(_('Ignoring DiskNotFound: %s') % exc,
                         instance=instance)
            except exception.VolumeNotFound as exc:
                LOG.warn(_('Ignoring VolumeNotFound: %s') % exc,
                         instance=instance)

        self._notify_about_instance_usage(context, instance, "shutdown.end")

    def _cleanup_volumes(self, context, instance_uuid, bdms):
        for bdm in bdms:
            LOG.debug(_("terminating bdm %s") % bdm,
                      instance_uuid=instance_uuid)
            if bdm['volume_id'] and bdm['delete_on_termination']:
                volume = self.volume_api.get(context, bdm['volume_id'])
                self.volume_api.delete(context, volume)
            # NOTE(vish): bdms will be deleted on instance destroy

    def _delete_instance(self, context, instance, bdms):
        """Delete an instance on this host."""
        instance_uuid = instance['uuid']
        self.db.instance_info_cache_delete(context, instance_uuid)
        self._notify_about_instance_usage(context, instance, "delete.start")
        self._shutdown_instance(context, instance, bdms)
        # NOTE(vish): We have already deleted the instance, so we have
        #             to ignore problems cleaning up the volumes. It would
        #             be nice to let the user know somehow that the volume
        #             deletion failed, but it is not acceptable to have an
        #             instance that can not be deleted. Perhaps this could
        #             be reworked in the future to set an instance fault
        #             the first time and to only ignore the failure if the
        #             instance is already in ERROR.
        try:
            self._cleanup_volumes(context, instance_uuid, bdms)
        except Exception as exc:
            LOG.warn(_("Ignoring volume cleanup failure due to %s") % exc,
                     instance_uuid=instance_uuid)
        # if a delete task succeed, always update vm state and task state
        # without expecting task state to be DELETING
        instance = self._instance_update(context,
                                         instance_uuid,
                                         vm_state=vm_states.DELETED,
                                         task_state=None,
                                         terminated_at=timeutils.utcnow())
        self.db.instance_destroy(context, instance_uuid)
        system_meta = self.db.instance_system_metadata_get(context,
            instance_uuid)

        # ensure block device mappings are not leaked
        for bdm in bdms:
            self.db.block_device_mapping_destroy(context, bdm['id'])

        self._notify_about_instance_usage(context, instance, "delete.end",
                system_metadata=system_meta)

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    @wrap_instance_fault
    def terminate_instance(self, context, instance, bdms=None):
        """Terminate an instance on this host.  """
        # Note(eglynn): we do not decorate this action with reverts_task_state
        # because a failure during termination should leave the task state as
        # DELETING, as a signal to the API layer that a subsequent deletion
        # attempt should not result in a further decrement of the quota_usages
        # in_use count (see bug 1046236).

        elevated = context.elevated()
        # NOTE(danms): remove this compatibility in the future
        if not bdms:
            bdms = self._get_instance_volume_bdms(context, instance["uuid"])

        @lockutils.synchronized(instance['uuid'], 'nova-')
        def do_terminate_instance(instance, bdms):
            try:
                self._delete_instance(context, instance, bdms)
            except exception.InstanceTerminationFailure as error:
                msg = _('%s. Setting instance vm_state to ERROR')
                LOG.error(msg % error, instance=instance)
                self._set_instance_error_state(context, instance['uuid'])
            except exception.InstanceNotFound as e:
                LOG.warn(e, instance=instance)

        do_terminate_instance(instance, bdms)

    # NOTE(johannes): This is probably better named power_off_instance
    # so it matches the driver method, but because of other issues, we
    # can't use that name in grizzly.
    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    @reverts_task_state
    @wrap_instance_fault
    def stop_instance(self, context, instance):
        """Stopping an instance on this host."""
        self._notify_about_instance_usage(context, instance, "power_off.start")
        self.driver.power_off(instance)
        current_power_state = self._get_power_state(context, instance)
        self._instance_update(context,
                              instance['uuid'],
                              power_state=current_power_state,
                              vm_state=vm_states.STOPPED,
                              expected_task_state=(task_states.POWERING_OFF,
                                                   task_states.STOPPING),
                              task_state=None)
        self._notify_about_instance_usage(context, instance, "power_off.end")

    # NOTE(johannes): This is probably better named power_on_instance
    # so it matches the driver method, but because of other issues, we
    # can't use that name in grizzly.
    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    @reverts_task_state
    @wrap_instance_fault
    def start_instance(self, context, instance):
        """Starting an instance on this host."""
        self._notify_about_instance_usage(context, instance, "power_on.start")
        self.driver.power_on(instance)
        current_power_state = self._get_power_state(context, instance)
        self._instance_update(context,
                              instance['uuid'],
                              power_state=current_power_state,
                              vm_state=vm_states.ACTIVE,
                              task_state=None,
                              expected_task_state=(task_states.POWERING_ON,
                                                   task_states.STARTING))
        self._notify_about_instance_usage(context, instance, "power_on.end")

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    @reverts_task_state
    @wrap_instance_fault
    def soft_delete_instance(self, context, instance):
        """Soft delete an instance on this host."""
        self._notify_about_instance_usage(context, instance,
                                          "soft_delete.start")
        try:
            self.driver.soft_delete(instance)
        except NotImplementedError:
            # Fallback to just powering off the instance if the hypervisor
            # doesn't implement the soft_delete method
            self.driver.power_off(instance)
        current_power_state = self._get_power_state(context, instance)
        self._instance_update(context,
                              instance['uuid'],
                              power_state=current_power_state,
                              vm_state=vm_states.SOFT_DELETED,
                              expected_task_state=task_states.SOFT_DELETING,
                              task_state=None)
        self._notify_about_instance_usage(context, instance, "soft_delete.end")

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    @reverts_task_state
    @wrap_instance_fault
    def restore_instance(self, context, instance):
        """Restore a soft-deleted instance on this host."""
        self._notify_about_instance_usage(context, instance, "restore.start")
        try:
            self.driver.restore(instance)
        except NotImplementedError:
            # Fallback to just powering on the instance if the hypervisor
            # doesn't implement the restore method
            self.driver.power_on(instance)
        current_power_state = self._get_power_state(context, instance)
        self._instance_update(context,
                              instance['uuid'],
                              power_state=current_power_state,
                              vm_state=vm_states.ACTIVE,
                              expected_task_state=task_states.RESTORING,
                              task_state=None)
        self._notify_about_instance_usage(context, instance, "restore.end")

    # NOTE(johannes): In the folsom release, power_off_instance was poorly
    # named. It was the main entry point to soft delete an instance. That
    # has been changed to soft_delete_instance now, but power_off_instance
    # will need to stick around for compatibility in grizzly.
    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    @reverts_task_state
    @wrap_instance_fault
    def power_off_instance(self, context, instance):
        """Power off an instance on this host."""
        self.soft_delete_instance(context, instance)

    # NOTE(johannes): In the folsom release, power_on_instance was poorly
    # named. It was the main entry point to restore a soft deleted instance.
    # That has been changed to restore_instance now, but power_on_instance
    # will need to stick around for compatibility in grizzly.
    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    @reverts_task_state
    @wrap_instance_fault
    def power_on_instance(self, context, instance):
        """Power on an instance on this host."""
        self.restore_instance(context, instance)

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    @reverts_task_state
    @wrap_instance_fault
    def rebuild_instance(self, context, instance, orig_image_ref, image_ref,
                         injected_files, new_pass, orig_sys_metadata=None):
        """Destroy and re-make this instance.

        A 'rebuild' effectively purges all existing data from the system and
        remakes the VM with given 'metadata' and 'personalities'.

        :param context: `nova.RequestContext` object
        :param instance: Instance dict
        :param orig_image_ref: Original image_ref before rebuild
        :param image_ref: New image_ref for rebuild
        :param injected_files: Files to inject
        :param new_pass: password to set on rebuilt instance
        :param orig_sys_metadata: instance system metadata from pre-rebuild
        """
        context = context.elevated()
        with self._error_out_instance_on_exception(context, instance['uuid']):
            LOG.audit(_("Rebuilding instance"), context=context,
                      instance=instance)

            image_meta = _get_image_meta(context, image_ref)

            # This instance.exists message should contain the original
            # image_ref, not the new one.  Since the DB has been updated
            # to point to the new one... we have to override it.
            orig_image_ref_url = utils.generate_image_url(orig_image_ref)
            extra_usage_info = {'image_ref_url': orig_image_ref_url}
            compute_utils.notify_usage_exists(context, instance,
                    current_period=True, system_metadata=orig_sys_metadata,
                    extra_usage_info=extra_usage_info)

            # This message should contain the new image_ref
            extra_usage_info = {'image_name': image_meta['name']}
            self._notify_about_instance_usage(context, instance,
                    "rebuild.start", extra_usage_info=extra_usage_info)

            current_power_state = self._get_power_state(context, instance)
            self._instance_update(context,
                                  instance['uuid'],
                                  power_state=current_power_state,
                                  task_state=task_states.REBUILDING,
                                  expected_task_state=task_states.REBUILDING)

            network_info = self._get_instance_nw_info(context, instance)
            self.driver.destroy(instance, self._legacy_nw_info(network_info))

            instance = self._instance_update(context,
                                  instance['uuid'],
                                  task_state=task_states.
                                      REBUILD_BLOCK_DEVICE_MAPPING,
                                  expected_task_state=task_states.REBUILDING)

            instance.injected_files = injected_files
            network_info = self.network_api.get_instance_nw_info(context,
                                                                 instance)
            device_info = self._setup_block_device_mapping(context, instance)

            instance = self._instance_update(context,
                                             instance['uuid'],
                                             task_state=task_states.
                                                 REBUILD_SPAWNING,
                                             expected_task_state=task_states.
                                                 REBUILD_BLOCK_DEVICE_MAPPING)
            # pull in new password here since the original password isn't in
            # the db
            admin_password = new_pass

            self.driver.spawn(context, instance, image_meta,
                              [], admin_password,
                              self._legacy_nw_info(network_info),
                              device_info)

            current_power_state = self._get_power_state(context, instance)
            instance = self._instance_update(context,
                                             instance['uuid'],
                                             power_state=current_power_state,
                                             vm_state=vm_states.ACTIVE,
                                             task_state=None,
                                             expected_task_state=task_states.
                                                 REBUILD_SPAWNING,
                                             launched_at=timeutils.utcnow())

            self._notify_about_instance_usage(
                    context, instance, "rebuild.end",
                    network_info=network_info,
                    extra_usage_info=extra_usage_info)

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    @reverts_task_state
    @wrap_instance_fault
    def reboot_instance(self, context, instance,
                        block_device_info=None,
                        network_info=None,
                        reboot_type="SOFT"):
        """Reboot an instance on this host."""
        context = context.elevated()
        LOG.audit(_("Rebooting instance"), context=context, instance=instance)

        # NOTE(danms): remove these when RPC API < 2.5 compatibility
        # is no longer needed
        if block_device_info is None:
            block_device_info = self._get_instance_volume_block_device_info(
                                context, instance['uuid'])
        # NOTE(danms): remove this when RPC API < 2.5 compatibility
        # is no longer needed
        if network_info is None:
            network_info = self._get_instance_nw_info(context, instance)
        else:
            network_info = network_model.NetworkInfo.hydrate(network_info)

        self._notify_about_instance_usage(context, instance, "reboot.start")

        current_power_state = self._get_power_state(context, instance)
        self._instance_update(context, instance['uuid'],
                              power_state=current_power_state,
                              vm_state=vm_states.ACTIVE)

        if instance['power_state'] != power_state.RUNNING:
            state = instance['power_state']
            running = power_state.RUNNING
            LOG.warn(_('trying to reboot a non-running '
                     'instance: (state: %(state)s '
                     'expected: %(running)s)') % locals(),
                     context=context, instance=instance)

        try:
            self.driver.reboot(instance, self._legacy_nw_info(network_info),
                               reboot_type, block_device_info)
        except Exception, exc:
            LOG.error(_('Cannot reboot instance: %(exc)s'), locals(),
                      context=context, instance=instance)
            compute_utils.add_instance_fault_from_exc(context,
                    instance['uuid'], exc, sys.exc_info())
            # Fall through and reset task_state to None

        current_power_state = self._get_power_state(context, instance)
        self._instance_update(context, instance['uuid'],
                              power_state=current_power_state,
                              vm_state=vm_states.ACTIVE,
                              task_state=None)

        self._notify_about_instance_usage(context, instance, "reboot.end")

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    @reverts_task_state
    @wrap_instance_fault
    def snapshot_instance(self, context, image_id, instance,
                          image_type='snapshot', backup_type=None,
                          rotation=None):
        """Snapshot an instance on this host.

        :param context: security context
        :param instance: an Instance dict
        :param image_id: glance.db.sqlalchemy.models.Image.Id
        :param image_type: snapshot | backup
        :param backup_type: daily | weekly
        :param rotation: int representing how many backups to keep around;
            None if rotation shouldn't be used (as in the case of snapshots)
        """
        context = context.elevated()

        current_power_state = self._get_power_state(context, instance)
        self._instance_update(context,
                              instance['uuid'],
                              power_state=current_power_state)

        LOG.audit(_('instance snapshotting'), context=context,
                  instance=instance)

        if instance['power_state'] != power_state.RUNNING:
            state = instance['power_state']
            running = power_state.RUNNING
            LOG.warn(_('trying to snapshot a non-running '
                       'instance: (state: %(state)s '
                       'expected: %(running)s)') % locals(),
                     instance=instance)

        self._notify_about_instance_usage(
                context, instance, "snapshot.start")

        self.driver.snapshot(context, instance, image_id)

        if image_type == 'snapshot':
            expected_task_state = task_states.IMAGE_SNAPSHOT

        elif image_type == 'backup':
            expected_task_state = task_states.IMAGE_BACKUP

        self._instance_update(context, instance['uuid'], task_state=None,
                              expected_task_state=expected_task_state)

        if image_type == 'snapshot' and rotation:
            raise exception.ImageRotationNotAllowed()

        elif image_type == 'backup' and rotation:
            self._rotate_backups(context, instance, backup_type, rotation)

        elif image_type == 'backup':
            raise exception.RotationRequiredForBackup()

        self._notify_about_instance_usage(
                context, instance, "snapshot.end")

    @wrap_instance_fault
    def _rotate_backups(self, context, instance, backup_type, rotation):
        """Delete excess backups associated to an instance.

        Instances are allowed a fixed number of backups (the rotation number);
        this method deletes the oldest backups that exceed the rotation
        threshold.

        :param context: security context
        :param instance: Instance dict
        :param backup_type: daily | weekly
        :param rotation: int representing how many backups to keep around;
            None if rotation shouldn't be used (as in the case of snapshots)
        """
        image_service = glance.get_default_image_service()
        filters = {'property-image_type': 'backup',
                   'property-backup_type': backup_type,
                   'property-instance_uuid': instance['uuid']}

        images = image_service.detail(context, filters=filters,
                                      sort_key='created_at', sort_dir='desc')
        num_images = len(images)
        LOG.debug(_("Found %(num_images)d images (rotation: %(rotation)d)"),
                  locals(), instance=instance)

        if num_images > rotation:
            # NOTE(sirp): this deletes all backups that exceed the rotation
            # limit
            excess = len(images) - rotation
            LOG.debug(_("Rotating out %d backups"), excess,
                      instance=instance)
            for i in xrange(excess):
                image = images.pop()
                image_id = image['id']
                LOG.debug(_("Deleting image %s"), image_id,
                          instance=instance)
                image_service.delete(context, image_id)

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    @reverts_task_state
    @wrap_instance_fault
    def set_admin_password(self, context, instance, new_pass=None):
        """Set the root/admin password for an instance on this host.

        This is generally only called by API password resets after an
        image has been built.
        """

        context = context.elevated()

        if new_pass is None:
            # Generate a random password
            new_pass = utils.generate_password(CONF.password_length)

        max_tries = 10

        for i in xrange(max_tries):
            current_power_state = self._get_power_state(context, instance)
            expected_state = power_state.RUNNING

            if current_power_state != expected_state:
                self._instance_update(context, instance['uuid'],
                                      task_state=None,
                                      expected_task_state=task_states.
                                          UPDATING_PASSWORD)
                _msg = _('Failed to set admin password. Instance %s is not'
                         ' running') % instance["uuid"]
                raise exception.InstancePasswordSetFailed(
                        instance=instance['uuid'], reason=_msg)
            else:
                try:
                    self.driver.set_admin_password(instance, new_pass)
                    LOG.audit(_("Root password set"), instance=instance)
                    self._instance_update(context,
                                          instance['uuid'],
                                          task_state=None,
                                          expected_task_state=task_states.
                                              UPDATING_PASSWORD)
                    break
                except NotImplementedError:
                    # NOTE(dprince): if the driver doesn't implement
                    # set_admin_password we break to avoid a loop
                    _msg = _('set_admin_password is not implemented '
                             'by this driver.')
                    LOG.warn(_msg, instance=instance)
                    self._instance_update(context,
                                          instance['uuid'],
                                          task_state=None,
                                          expected_task_state=task_states.
                                              UPDATING_PASSWORD)
                    raise exception.InstancePasswordSetFailed(
                            instance=instance['uuid'], reason=_msg)
                except exception.UnexpectedTaskStateError:
                    # interrupted by another (most likely delete) task
                    # do not retry
                    raise
                except Exception, e:
                    # Catch all here because this could be anything.
                    LOG.exception(_('set_admin_password failed: %s') % e,
                                  instance=instance)
                    if i == max_tries - 1:
                        self._set_instance_error_state(context,
                                                       instance['uuid'])
                        # We create a new exception here so that we won't
                        # potentially reveal password information to the
                        # API caller.  The real exception is logged above
                        _msg = _('error setting admin password')
                        raise exception.InstancePasswordSetFailed(
                                instance=instance['uuid'], reason=_msg)
                    time.sleep(1)
                    continue

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    @reverts_task_state
    @wrap_instance_fault
    def inject_file(self, context, path, file_contents, instance):
        """Write a file to the specified path in an instance on this host."""
        context = context.elevated()
        current_power_state = self._get_power_state(context, instance)
        expected_state = power_state.RUNNING
        if current_power_state != expected_state:
            LOG.warn(_('trying to inject a file into a non-running '
                    '(state: %(current_power_state)s '
                    'expected: %(expected_state)s)') % locals(),
                     instance=instance)
        LOG.audit(_('injecting file to %(path)s') % locals(),
                    instance=instance)
        self.driver.inject_file(instance, path, file_contents)

    def _get_rescue_image_ref(self, context, instance):
        """Determine what image should be used to boot the rescue VM. """
        system_meta = self.db.instance_system_metadata_get(
                context, instance['uuid'])

        rescue_image_ref = system_meta.get('image_base_image_ref')

        # 1. First try to use base image associated with instance's current
        #    image.
        #
        # The idea here is to provide the customer with a rescue environment
        # which they are familiar with. So, if they built their instance off of
        # a Debian image, their rescue VM wil also be Debian.
        if rescue_image_ref:
            return rescue_image_ref

        # 2. As a last resort, use instance's current image
        LOG.warn(_('Unable to find a different image to use for rescue VM,'
                   ' using instance\'s current image'))
        return instance['image_ref']

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    @reverts_task_state
    @wrap_instance_fault
    def rescue_instance(self, context, instance, rescue_password=None):
        """
        Rescue an instance on this host.
        :param rescue_password: password to set on rescue instance
        """
        context = context.elevated()
        LOG.audit(_('Rescuing'), context=context, instance=instance)

        admin_password = (rescue_password if rescue_password else
                      utils.generate_password(CONF.password_length))

        network_info = self._get_instance_nw_info(context, instance)

        # Boot the instance using the 'base' image instead of the user's
        # current (possibly broken) image
        rescue_image_ref = self._get_rescue_image_ref(context, instance)
        rescue_image_meta = _get_image_meta(context, rescue_image_ref)

        with self._error_out_instance_on_exception(context, instance['uuid']):
            self.driver.rescue(context, instance,
                               self._legacy_nw_info(network_info),
                               rescue_image_meta, admin_password)

        current_power_state = self._get_power_state(context, instance)
        self._instance_update(context,
                              instance['uuid'],
                              vm_state=vm_states.RESCUED,
                              task_state=None,
                              power_state=current_power_state,
                              expected_task_state=task_states.RESCUING)

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    @reverts_task_state
    @wrap_instance_fault
    def unrescue_instance(self, context, instance):
        """Rescue an instance on this host."""
        context = context.elevated()
        LOG.audit(_('Unrescuing'), context=context, instance=instance)

        network_info = self._get_instance_nw_info(context, instance)

        with self._error_out_instance_on_exception(context, instance['uuid']):
            self.driver.unrescue(instance,
                                 self._legacy_nw_info(network_info))

        current_power_state = self._get_power_state(context, instance)
        self._instance_update(context,
                              instance['uuid'],
                              vm_state=vm_states.ACTIVE,
                              task_state=None,
                              expected_task_state=task_states.UNRESCUING,
                              power_state=current_power_state)

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    @reverts_task_state
    @wrap_instance_fault
    def change_instance_metadata(self, context, diff, instance):
        """Update the metadata published to the instance."""
        LOG.debug(_("Changing instance metadata according to %(diff)r") %
                  locals(), instance=instance)
        self.driver.change_instance_metadata(context, instance, diff)

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    @wrap_instance_fault
    def confirm_resize(self, context, instance, reservations=None,
                       migration=None, migration_id=None):
        """Destroys the source instance."""
        if not migration:
            migration = self.db.migration_get(context.elevated(),
                    migration_id)

        self._notify_about_instance_usage(context, instance,
                                          "resize.confirm.start")

        with self._error_out_instance_on_exception(context, instance['uuid'],
                                                   reservations):
            # NOTE(tr3buchet): tear down networks on source host
            self.network_api.setup_networks_on_host(context, instance,
                               migration['source_compute'], teardown=True)

            network_info = self._get_instance_nw_info(context, instance)
            self.driver.confirm_migration(migration, instance,
                                          self._legacy_nw_info(network_info))

            self._notify_about_instance_usage(
                context, instance, "resize.confirm.end",
                network_info=network_info)

            self._quota_commit(context, reservations)

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    @reverts_task_state
    @wrap_instance_fault
    def revert_resize(self, context, instance, migration=None,
                      migration_id=None, reservations=None):
        """Destroys the new instance on the destination machine.

        Reverts the model changes, and powers on the old instance on the
        source machine.

        """
        if not migration:
            migration = self.db.migration_get(context.elevated(),
                                                  migration_id)

        # NOTE(comstud): A revert_resize is essentially a resize back to
        # the old size, so we need to send a usage event here.
        compute_utils.notify_usage_exists(
                context, instance, current_period=True)

        with self._error_out_instance_on_exception(context, instance['uuid'],
                                                   reservations):
            # NOTE(tr3buchet): tear down networks on destination host
            self.network_api.setup_networks_on_host(context, instance,
                                                    teardown=True)

            self.network_api.migrate_instance_start(context, instance,
                                                    migration)

            network_info = self._get_instance_nw_info(context, instance)
            block_device_info = self._get_instance_volume_block_device_info(
                                context, instance['uuid'])

            self.driver.destroy(instance, self._legacy_nw_info(network_info),
                                block_device_info)

            self._terminate_volume_connections(context, instance)

            self.compute_rpcapi.finish_revert_resize(context, instance,
                    migration, migration['source_compute'],
                    reservations)

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    @reverts_task_state
    @wrap_instance_fault
    def finish_revert_resize(self, context, instance, reservations=None,
                             migration=None, migration_id=None):
        """Finishes the second half of reverting a resize.

        Power back on the source instance and revert the resized attributes
        in the database.

        """
        elevated = context.elevated()

        if not migration:
            migration = self.db.migration_get(elevated, migration_id)

        with self._error_out_instance_on_exception(context, instance['uuid'],
                                                   reservations):
            network_info = self._get_instance_nw_info(context, instance)

            self._notify_about_instance_usage(
                    context, instance, "resize.revert.start")

            instance = self._instance_update(context,
                                        instance['uuid'],
                                        host=migration['source_compute'])
            self.network_api.setup_networks_on_host(context, instance,
                                            migration['source_compute'])

            old_instance_type = migration['old_instance_type_id']
            instance_type = instance_types.get_instance_type(old_instance_type)

            bdms = self._get_instance_volume_bdms(context, instance['uuid'])
            block_device_info = self._get_instance_volume_block_device_info(
                    context, instance['uuid'])
            if bdms:
                connector = self.driver.get_volume_connector(instance)
                for bdm in bdms:
                    volume = self.volume_api.get(context, bdm['volume_id'])
                    self.volume_api.initialize_connection(context, volume,
                                                          connector)

            self.driver.finish_revert_migration(instance,
                                       self._legacy_nw_info(network_info),
                                       block_device_info)

            # Just roll back the record. There's no need to resize down since
            # the 'old' VM already has the preferred attributes
            self._instance_update(context,
                                  instance['uuid'],
                                  memory_mb=instance_type['memory_mb'],
                                  vcpus=instance_type['vcpus'],
                                  root_gb=instance_type['root_gb'],
                                  ephemeral_gb=instance_type['ephemeral_gb'],
                                  instance_type_id=instance_type['id'],
                                  launched_at=timeutils.utcnow(),
                                  expected_task_state=task_states.
                                      RESIZE_REVERTING)

            self.network_api.migrate_instance_finish(context, instance,
                                                     migration)

            self._instance_update(context, instance['uuid'],
                                  vm_state=vm_states.ACTIVE,
                                  task_state=None)

            self.db.migration_update(elevated, migration['id'],
                    {'status': 'reverted'})

            self._notify_about_instance_usage(
                    context, instance, "resize.revert.end")

            self._quota_commit(context, reservations)

    @staticmethod
    def _quota_commit(context, reservations):
        if reservations:
            QUOTAS.commit(context, reservations)

    @staticmethod
    def _quota_rollback(context, reservations):
        if reservations:
            QUOTAS.rollback(context, reservations)

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    @reverts_task_state
    @wrap_instance_fault
    def prep_resize(self, context, image, instance, instance_type,
                    reservations=None, request_spec=None,
                    filter_properties=None):
        """Initiates the process of moving a running instance to another host.

        Possibly changes the RAM and disk size in the process.

        """
        with self._error_out_instance_on_exception(context, instance['uuid'],
                                                   reservations):
            compute_utils.notify_usage_exists(
                    context, instance, current_period=True)
            self._notify_about_instance_usage(
                    context, instance, "resize.prep.start")

            try:
                same_host = instance['host'] == self.host
                if same_host and not CONF.allow_resize_to_same_host:
                    self._set_instance_error_state(context, instance['uuid'])
                    msg = _('destination same as source!')
                    raise exception.MigrationError(msg)

                old_instance_type = instance['instance_type']

                migration_ref = self.db.migration_create(context.elevated(),
                        {'instance_uuid': instance['uuid'],
                         'source_compute': instance['host'],
                         'dest_compute': self.host,
                         'dest_host': self.driver.get_host_ip_addr(),
                         'old_instance_type_id': old_instance_type['id'],
                         'new_instance_type_id': instance_type['id'],
                         'status': 'pre-migrating'})

                LOG.audit(_('Migrating'), context=context,
                        instance=instance)
                self.compute_rpcapi.resize_instance(context, instance,
                        migration_ref, image, instance_type, reservations)

            except Exception:
                # try to re-schedule the resize elsewhere:
                self._reschedule_resize_or_reraise(context, image, instance,
                        instance_type, reservations, request_spec,
                        filter_properties)
            finally:
                extra_usage_info = dict(
                        new_instance_type=instance_type['name'],
                        new_instance_type_id=instance_type['id'])

                self._notify_about_instance_usage(
                    context, instance, "resize.prep.end",
                    extra_usage_info=extra_usage_info)

    def _reschedule_resize_or_reraise(self, context, image, instance,
            instance_type, reservations, request_spec, filter_properties):
        """Try to re-schedule the resize or re-raise the original error to
        error out the instance.
        """
        if not request_spec:
            request_spec = {}
        if not filter_properties:
            filter_properties = {}

        exc_info = sys.exc_info()
        rescheduled = False
        instance_uuid = instance['uuid']

        try:
            scheduler_method = self.scheduler_rpcapi.prep_resize
            method_args = (instance, instance_type, image, request_spec,
                           filter_properties, reservations)
            task_state = task_states.RESIZE_PREP

            rescheduled = self._reschedule(context, request_spec,
                    filter_properties, instance_uuid, scheduler_method,
                    method_args, task_state)
        except Exception:
            rescheduled = False
            LOG.exception(_("Error trying to reschedule"),
                          instance_uuid=instance_uuid)

        if rescheduled:
            # log the original build error
            self._log_original_error(exc_info, instance_uuid)
        else:
            # not re-scheduling
            raise exc_info[0], exc_info[1], exc_info[2]

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    @reverts_task_state
    @wrap_instance_fault
    def resize_instance(self, context, instance, image,
                        reservations=None, migration=None, migration_id=None,
                        instance_type=None):
        """Starts the migration of a running instance to another host."""
        elevated = context.elevated()
        if not migration:
            migration = self.db.migration_get(elevated, migration_id)
        with self._error_out_instance_on_exception(context, instance['uuid'],
                                                   reservations):
            if not instance_type:
                instance_type = self.db.instance_type_get(context,
                        migration['new_instance_type_id'])

            network_info = self._get_instance_nw_info(context, instance)

            self.db.migration_update(elevated,
                                     migration['id'],
                                     {'status': 'migrating'})

            instance = self._instance_update(context, instance['uuid'],
                    task_state=task_states.RESIZE_MIGRATING,
                    expected_task_state=task_states.RESIZE_PREP)

            self._notify_about_instance_usage(
                context, instance, "resize.start", network_info=network_info)

            block_device_info = self._get_instance_volume_block_device_info(
                                context, instance['uuid'])

            disk_info = self.driver.migrate_disk_and_power_off(
                    context, instance, migration['dest_host'],
                    instance_type, self._legacy_nw_info(network_info),
                    block_device_info)

            self._terminate_volume_connections(context, instance)

            self.network_api.migrate_instance_start(context, instance,
                                                    migration)

            migration = self.db.migration_update(elevated,
                                                 migration['id'],
                                                 {'status': 'post-migrating'})

            instance = self._instance_update(context, instance['uuid'],
                    host=migration['dest_compute'],
                    task_state=task_states.RESIZE_MIGRATED,
                    expected_task_state=task_states.
                    RESIZE_MIGRATING)

            self.compute_rpcapi.finish_resize(context, instance,
                    migration, image, disk_info,
                    migration['dest_compute'], reservations)

            self._notify_about_instance_usage(context, instance, "resize.end",
                                              network_info=network_info)

    def _terminate_volume_connections(self, context, instance):
        bdms = self._get_instance_volume_bdms(context, instance['uuid'])
        if bdms:
            connector = self.driver.get_volume_connector(instance)
            for bdm in bdms:
                volume = self.volume_api.get(context, bdm['volume_id'])
                self.volume_api.terminate_connection(context, volume,
                        connector)

    def _finish_resize(self, context, instance, migration, disk_info,
                       image):
        resize_instance = False
        old_instance_type_id = migration['old_instance_type_id']
        new_instance_type_id = migration['new_instance_type_id']
        if old_instance_type_id != new_instance_type_id:
            instance_type = instance_types.get_instance_type(
                    new_instance_type_id)
            instance = self._instance_update(
                    context,
                    instance['uuid'],
                    instance_type_id=instance_type['id'],
                    memory_mb=instance_type['memory_mb'],
                    vcpus=instance_type['vcpus'],
                    root_gb=instance_type['root_gb'],
                    ephemeral_gb=instance_type['ephemeral_gb'])
            resize_instance = True

        # NOTE(tr3buchet): setup networks on destination host
        self.network_api.setup_networks_on_host(context, instance,
                                                migration['dest_compute'])

        self.network_api.migrate_instance_finish(context, instance,
                                                 migration)

        network_info = self._get_instance_nw_info(context, instance)

        instance = self._instance_update(context, instance['uuid'],
                              task_state=task_states.RESIZE_FINISH,
                              expected_task_state=task_states.RESIZE_MIGRATED)

        self._notify_about_instance_usage(
            context, instance, "finish_resize.start",
            network_info=network_info)

        bdms = self._get_instance_volume_bdms(context, instance['uuid'])
        block_device_info = self._get_instance_volume_block_device_info(
                            context, instance['uuid'], bdms=bdms)

        if bdms:
            connector = self.driver.get_volume_connector(instance)
            for bdm in bdms:
                volume = self.volume_api.get(context, bdm['volume_id'])
                self.volume_api.initialize_connection(context, volume,
                                                      connector)

        self.driver.finish_migration(context, migration, instance,
                                     disk_info,
                                     self._legacy_nw_info(network_info),
                                     image, resize_instance,
                                     block_device_info)

        instance = self._instance_update(context,
                                         instance['uuid'],
                                         vm_state=vm_states.RESIZED,
                                         launched_at=timeutils.utcnow(),
                                         task_state=None,
                                         expected_task_state=task_states.
                                             RESIZE_FINISH)

        self.db.migration_update(context.elevated(), migration['id'],
                                 {'status': 'finished'})

        self._notify_about_instance_usage(
            context, instance, "finish_resize.end",
            network_info=network_info)

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    @reverts_task_state
    @wrap_instance_fault
    def finish_resize(self, context, disk_info, image, instance,
                      reservations=None, migration=None, migration_id=None):
        """Completes the migration process.

        Sets up the newly transferred disk and turns on the instance at its
        new host machine.

        """
        if not migration:
            migration = self.db.migration_get(context.elevated(),
                    migration_id)
        try:
            self._finish_resize(context, instance, migration,
                                disk_info, image)
            self._quota_commit(context, reservations)
        except Exception as error:
            with excutils.save_and_reraise_exception():
                try:
                    self._quota_rollback(context, reservations)
                except Exception as qr_error:
                    reason = _("Failed to rollback quota for failed "
                            "finish_resize: %(qr_error)s")
                    LOG.exception(reason % locals(), instance=instance)
                LOG.error(_('%s. Setting instance vm_state to ERROR') % error,
                          instance=instance)
                self._set_instance_error_state(context, instance['uuid'])

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    @reverts_task_state
    @wrap_instance_fault
    def add_fixed_ip_to_instance(self, context, network_id, instance):
        """Calls network_api to add new fixed_ip to instance
        then injects the new network info and resets instance networking.

        """
        self._notify_about_instance_usage(
                context, instance, "create_ip.start")

        self.network_api.add_fixed_ip_to_instance(context,
                                                  instance,
                                                  network_id)

        network_info = self._inject_network_info(context, instance=instance)
        self.reset_network(context, instance)

        self._notify_about_instance_usage(
            context, instance, "create_ip.end", network_info=network_info)

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    @reverts_task_state
    @wrap_instance_fault
    def remove_fixed_ip_from_instance(self, context, address, instance):
        """Calls network_api to remove existing fixed_ip from instance
        by injecting the altered network info and resetting
        instance networking.
        """
        self._notify_about_instance_usage(
                context, instance, "delete_ip.start")

        self.network_api.remove_fixed_ip_from_instance(context,
                                                       instance,
                                                       address)

        network_info = self._inject_network_info(context,
                                                 instance=instance)
        self.reset_network(context, instance)

        self._notify_about_instance_usage(
            context, instance, "delete_ip.end", network_info=network_info)

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    @reverts_task_state
    @wrap_instance_fault
    def pause_instance(self, context, instance):
        """Pause an instance on this host."""
        context = context.elevated()
        LOG.audit(_('Pausing'), context=context, instance=instance)
        self.driver.pause(instance)

        current_power_state = self._get_power_state(context, instance)
        self._instance_update(context,
                              instance['uuid'],
                              power_state=current_power_state,
                              vm_state=vm_states.PAUSED,
                              task_state=None,
                              expected_task_state=task_states.PAUSING)

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    @reverts_task_state
    @wrap_instance_fault
    def unpause_instance(self, context, instance):
        """Unpause a paused instance on this host."""
        context = context.elevated()
        LOG.audit(_('Unpausing'), context=context, instance=instance)
        self.driver.unpause(instance)

        current_power_state = self._get_power_state(context, instance)
        self._instance_update(context,
                              instance['uuid'],
                              power_state=current_power_state,
                              vm_state=vm_states.ACTIVE,
                              task_state=None,
                              expected_task_state=task_states.UNPAUSING)

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    def host_power_action(self, context, host=None, action=None):
        """Reboots, shuts down or powers up the host."""
        return self.driver.host_power_action(host, action)

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    def host_maintenance_mode(self, context, host, mode):
        """Start/Stop host maintenance window. On start, it triggers
        guest VMs evacuation."""
        return self.driver.host_maintenance_mode(host, mode)

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    def set_host_enabled(self, context, host=None, enabled=None):
        """Sets the specified host's ability to accept new instances."""
        return self.driver.set_host_enabled(host, enabled)

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    def get_host_uptime(self, context, host):
        """Returns the result of calling "uptime" on the target host."""
        return self.driver.get_host_uptime(host)

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    @wrap_instance_fault
    def get_diagnostics(self, context, instance):
        """Retrieve diagnostics for an instance on this host."""
        current_power_state = self._get_power_state(context, instance)
        if current_power_state == power_state.RUNNING:
            LOG.audit(_("Retrieving diagnostics"), context=context,
                      instance=instance)
            return self.driver.get_diagnostics(instance)

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    @reverts_task_state
    @wrap_instance_fault
    def suspend_instance(self, context, instance):
        """Suspend the given instance."""
        context = context.elevated()

        with self._error_out_instance_on_exception(context, instance['uuid']):
            self.driver.suspend(instance)

        current_power_state = self._get_power_state(context, instance)
        self._instance_update(context,
                              instance['uuid'],
                              power_state=current_power_state,
                              vm_state=vm_states.SUSPENDED,
                              task_state=None,
                              expected_task_state=task_states.SUSPENDING)

        self._notify_about_instance_usage(context, instance, 'suspend')

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    @reverts_task_state
    @wrap_instance_fault
    def resume_instance(self, context, instance):
        """Resume the given suspended instance."""
        context = context.elevated()
        LOG.audit(_('Resuming'), context=context, instance=instance)
        self.driver.resume(instance)

        current_power_state = self._get_power_state(context, instance)
        self._instance_update(context,
                              instance['uuid'],
                              power_state=current_power_state,
                              vm_state=vm_states.ACTIVE,
                              task_state=None)

        self._notify_about_instance_usage(context, instance, 'resume')

    @reverts_task_state
    @wrap_instance_fault
    def reset_network(self, context, instance):
        """Reset networking on the given instance."""
        LOG.debug(_('Reset network'), context=context, instance=instance)
        self.driver.reset_network(instance)

    def _inject_network_info(self, context, instance):
        """Inject network info for the given instance."""
        LOG.debug(_('Inject network info'), context=context, instance=instance)

        network_info = self._get_instance_nw_info(context, instance)
        LOG.debug(_('network_info to inject: |%s|'), network_info,
                  instance=instance)

        self.driver.inject_network_info(instance,
                                        self._legacy_nw_info(network_info))
        return network_info

    @wrap_instance_fault
    def inject_network_info(self, context, instance):
        """Inject network info, but don't return the info."""
        self._inject_network_info(context, instance)

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    @wrap_instance_fault
    def get_console_output(self, context, instance, tail_length=None):
        """Send the console output for the given instance."""
        context = context.elevated()
        LOG.audit(_("Get console output"), context=context,
                  instance=instance)
        output = self.driver.get_console_output(instance)

        if tail_length is not None:
            output = self._tail_log(output, tail_length)

        return output.decode('utf-8', 'replace').encode('ascii', 'replace')

    def _tail_log(self, log, length):
        try:
            length = int(length)
        except ValueError:
            length = 0

        if length == 0:
            return ''
        else:
            return '\n'.join(log.split('\n')[-int(length):])

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    @wrap_instance_fault
    def get_vnc_console(self, context, console_type, instance):
        """Return connection information for a vnc console."""
        context = context.elevated()
        LOG.debug(_("Getting vnc console"), instance=instance)
        token = str(uuid.uuid4())

        if console_type == 'novnc':
            # For essex, novncproxy_base_url must include the full path
            # including the html file (like http://myhost/vnc_auto.html)
            access_url = '%s?token=%s' % (CONF.novncproxy_base_url, token)
        elif console_type == 'xvpvnc':
            access_url = '%s?token=%s' % (CONF.xvpvncproxy_base_url, token)
        else:
            raise exception.ConsoleTypeInvalid(console_type=console_type)

        # Retrieve connect info from driver, and then decorate with our
        # access info token
        connect_info = self.driver.get_vnc_console(instance)
        connect_info['token'] = token
        connect_info['access_url'] = access_url

        return connect_info

    def _attach_volume_boot(self, context, instance, volume, mountpoint):
        """Attach a volume to an instance at boot time. So actual attach
        is done by instance creation"""

        instance_id = instance['id']
        instance_uuid = instance['uuid']
        volume_id = volume['id']
        context = context.elevated()
        LOG.audit(_('Booting with volume %(volume_id)s at %(mountpoint)s'),
                  locals(), context=context, instance=instance)
        connector = self.driver.get_volume_connector(instance)
        connection_info = self.volume_api.initialize_connection(context,
                                                                volume,
                                                                connector)
        self.volume_api.attach(context, volume, instance_uuid, mountpoint)
        return connection_info

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    @reverts_task_state
    @wrap_instance_fault
    def reserve_block_device_name(self, context, instance, device,
                                  volume_id=None):

        @lockutils.synchronized(instance['uuid'], 'nova-')
        def do_reserve():
            result = compute_utils.get_device_name_for_instance(context,
                                                                instance,
                                                                device)
            # NOTE(vish): create bdm here to avoid race condition
            values = {'instance_uuid': instance['uuid'],
                      'volume_id': volume_id or 'reserved',
                      'device_name': result}
            self.db.block_device_mapping_create(context, values)
            return result
        return do_reserve()

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    @reverts_task_state
    @wrap_instance_fault
    def attach_volume(self, context, volume_id, mountpoint, instance):
        """Attach a volume to an instance."""
        try:
            return self._attach_volume(context, volume_id,
                                       mountpoint, instance)
        except Exception:
            with excutils.save_and_reraise_exception():
                self.db.block_device_mapping_destroy_by_instance_and_device(
                        context, instance.get('uuid'), mountpoint)

    def _attach_volume(self, context, volume_id, mountpoint, instance):
        volume = self.volume_api.get(context, volume_id)
        context = context.elevated()
        LOG.audit(_('Attaching volume %(volume_id)s to %(mountpoint)s'),
                  locals(), context=context, instance=instance)
        try:
            connector = self.driver.get_volume_connector(instance)
            connection_info = self.volume_api.initialize_connection(context,
                                                                    volume,
                                                                    connector)
        except Exception:  # pylint: disable=W0702
            with excutils.save_and_reraise_exception():
                msg = _("Failed to connect to volume %(volume_id)s "
                        "while attaching at %(mountpoint)s")
                LOG.exception(msg % locals(), context=context,
                              instance=instance)
                self.volume_api.unreserve_volume(context, volume)

        if 'serial' not in connection_info:
            connection_info['serial'] = volume_id

        try:
            self.driver.attach_volume(connection_info,
                                      instance['name'],
                                      mountpoint)
        except Exception:  # pylint: disable=W0702
            with excutils.save_and_reraise_exception():
                msg = _("Failed to attach volume %(volume_id)s "
                        "at %(mountpoint)s")
                LOG.exception(msg % locals(), context=context,
                              instance=instance)
                self.volume_api.terminate_connection(context,
                                                     volume,
                                                     connector)

        self.volume_api.attach(context,
                               volume,
                               instance['uuid'],
                               mountpoint)
        values = {
            'instance_uuid': instance['uuid'],
            'connection_info': jsonutils.dumps(connection_info),
            'device_name': mountpoint,
            'delete_on_termination': False,
            'virtual_name': None,
            'snapshot_id': None,
            'volume_id': volume_id,
            'volume_size': None,
            'no_device': None}
        self.db.block_device_mapping_update_or_create(context, values)

    def _detach_volume(self, context, instance, bdm):
        """Do the actual driver detach using block device mapping."""
        mp = bdm['device_name']
        volume_id = bdm['volume_id']

        LOG.audit(_('Detach volume %(volume_id)s from mountpoint %(mp)s'),
                  locals(), context=context, instance=instance)

        if not self.driver.instance_exists(instance['name']):
            LOG.warn(_('Detaching volume from unknown instance'),
                     context=context, instance=instance)
        connection_info = jsonutils.loads(bdm['connection_info'])
        # NOTE(vish): We currently don't use the serial when disconnecting,
        #             but added for completeness in case we ever do.
        if connection_info and 'serial' not in connection_info:
            connection_info['serial'] = volume_id
        try:
            self.driver.detach_volume(connection_info,
                                      instance['name'],
                                      mp)
        except Exception:  # pylint: disable=W0702
            with excutils.save_and_reraise_exception():
                msg = _("Faild to detach volume %(volume_id)s from %(mp)s")
                LOG.exception(msg % locals(), context=context,
                              instance=instance)
                volume = self.volume_api.get(context, volume_id)
                self.volume_api.roll_detaching(context, volume)

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    @reverts_task_state
    @wrap_instance_fault
    def detach_volume(self, context, volume_id, instance):
        """Detach a volume from an instance."""
        bdm = self._get_instance_volume_bdm(context, instance['uuid'],
                                            volume_id)
        self._detach_volume(context, instance, bdm)
        volume = self.volume_api.get(context, volume_id)
        connector = self.driver.get_volume_connector(instance)
        self.volume_api.terminate_connection(context, volume, connector)
        self.volume_api.detach(context.elevated(), volume)
        self.db.block_device_mapping_destroy_by_instance_and_volume(
            context, instance['uuid'], volume_id)

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    def remove_volume_connection(self, context, volume_id, instance):
        """Remove a volume connection using the volume api"""
        # NOTE(vish): We don't want to actually mark the volume
        #             detached, or delete the bdm, just remove the
        #             connection from this host.
        try:
            bdm = self._get_instance_volume_bdm(context,
                                                instance['uuid'],
                                                volume_id)
            self._detach_volume(context, instance, bdm)
            volume = self.volume_api.get(context, volume_id)
            connector = self.driver.get_volume_connector(instance)
            self.volume_api.terminate_connection(context, volume, connector)
        except exception.NotFound:
            pass

    def _get_compute_info(self, context, host):
        compute_node_ref = self.db.service_get_all_compute_by_host(context,
                                                                   host)
        try:
            return compute_node_ref[0]['compute_node'][0]
        except IndexError:
            raise exception.NotFound(_("Host %(host)s not found") % locals())

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    def check_can_live_migrate_destination(self, ctxt, instance,
                                           block_migration=False,
                                           disk_over_commit=False):
        """Check if it is possible to execute live migration.

        This runs checks on the destination host, and then calls
        back to the source host to check the results.

        :param context: security context
        :param instance: dict of instance data
        :param block_migration: if true, prepare for block migration
        :param disk_over_commit: if true, allow disk over commit

        Returns a mapping of values required in case of block migration
        and None otherwise.
        """
        src_compute_info = self._get_compute_info(ctxt, instance['host'])
        dst_compute_info = self._get_compute_info(ctxt, CONF.host)
        dest_check_data = self.driver.check_can_live_migrate_destination(ctxt,
            instance, src_compute_info, dst_compute_info,
            block_migration, disk_over_commit)
        try:
            self.compute_rpcapi.check_can_live_migrate_source(ctxt,
                    instance, dest_check_data)
        finally:
            self.driver.check_can_live_migrate_destination_cleanup(ctxt,
                    dest_check_data)
        if dest_check_data and 'migrate_data' in dest_check_data:
            return dest_check_data['migrate_data']

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    def check_can_live_migrate_source(self, ctxt, instance, dest_check_data):
        """Check if it is possible to execute live migration.

        This checks if the live migration can succeed, based on the
        results from check_can_live_migrate_destination.

        :param context: security context
        :param instance: dict of instance data
        :param dest_check_data: result of check_can_live_migrate_destination
        """
        self.driver.check_can_live_migrate_source(ctxt, instance,
                                                  dest_check_data)

    def pre_live_migration(self, context, instance,
                           block_migration=False, disk=None):
        """Preparations for live migration at dest host.

        :param context: security context
        :param instance: dict of instance data
        :param block_migration: if true, prepare for block migration

        """
        # If any volume is mounted, prepare here.
        block_device_info = self._get_instance_volume_block_device_info(
                            context, instance['uuid'])
        if not block_device_info['block_device_mapping']:
            LOG.info(_('Instance has no volume.'), instance=instance)

        # assign the volume to host system
        # needed by the lefthand volume driver and maybe others
        connector = self.driver.get_volume_connector(instance)
        for bdm in self._get_instance_volume_bdms(context, instance['uuid']):
            volume = self.volume_api.get(context, bdm['volume_id'])
            self.volume_api.initialize_connection(context, volume, connector)

        network_info = self._get_instance_nw_info(context, instance)

        # TODO(tr3buchet): figure out how on the earth this is necessary
        fixed_ips = network_info.fixed_ips()
        if not fixed_ips:
            raise exception.FixedIpNotFoundForInstance(
                                       instance_uuid=instance['uuid'])

        self.driver.pre_live_migration(context, instance,
                                       block_device_info,
                                       self._legacy_nw_info(network_info))

        # NOTE(tr3buchet): setup networks on destination host
        self.network_api.setup_networks_on_host(context, instance,
                                                         self.host)

        # Creating filters to hypervisors and firewalls.
        # An example is that nova-instance-instance-xxx,
        # which is written to libvirt.xml(Check "virsh nwfilter-list")
        # This nwfilter is necessary on the destination host.
        # In addition, this method is creating filtering rule
        # onto destination host.
        self.driver.ensure_filtering_rules_for_instance(instance,
                                            self._legacy_nw_info(network_info))

        # Preparation for block migration
        if block_migration:
            self.driver.pre_block_migration(context, instance, disk)

    def live_migration(self, context, dest, instance,
                       block_migration=False, migrate_data=None):
        """Executing live migration.

        :param context: security context
        :param instance: instance dict
        :param dest: destination host
        :param block_migration: if true, prepare for block migration
        :param migrate_data: implementation specific params

        """
        try:
            if block_migration:
                disk = self.driver.get_instance_disk_info(instance['name'])
            else:
                disk = None

            self.compute_rpcapi.pre_live_migration(context, instance,
                    block_migration, disk, dest)

        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.exception(_('Pre live migration failed at  %(dest)s'),
                              locals(), instance=instance)
                self._rollback_live_migration(context, instance, dest,
                                              block_migration)

        # Executing live migration
        # live_migration might raises exceptions, but
        # nothing must be recovered in this version.
        self.driver.live_migration(context, instance, dest,
                                   self._post_live_migration,
                                   self._rollback_live_migration,
                                   block_migration, migrate_data)

    def _post_live_migration(self, ctxt, instance_ref,
                            dest, block_migration=False):
        """Post operations for live migration.

        This method is called from live_migration
        and mainly updating database record.

        :param ctxt: security context
        :param instance_ref: nova.db.sqlalchemy.models.Instance
        :param dest: destination host
        :param block_migration: if true, prepare for block migration

        """
        LOG.info(_('_post_live_migration() is started..'),
                 instance=instance_ref)

        # Detaching volumes.
        connector = self.driver.get_volume_connector(instance_ref)
        for bdm in self._get_instance_volume_bdms(ctxt, instance_ref['uuid']):
            # NOTE(vish): We don't want to actually mark the volume
            #             detached, or delete the bdm, just remove the
            #             connection from this host.

            # remove the volume connection without detaching from hypervisor
            # because the instance is not running anymore on the current host
            volume = self.volume_api.get(ctxt, bdm['volume_id'])
            self.volume_api.terminate_connection(ctxt, volume, connector)

        # Releasing vlan.
        # (not necessary in current implementation?)

        network_info = self._get_instance_nw_info(ctxt, instance_ref)
        # Releasing security group ingress rule.
        self.driver.unfilter_instance(instance_ref,
                                      self._legacy_nw_info(network_info))

        # Database updating.
        # NOTE(jkoelker) This needs to be converted to network api calls
        #                if nova wants to support floating_ips in
        #                quantum/melange
        try:
            # Not return if floating_ip is not found, otherwise,
            # instance never be accessible..
            floating_ip = self.db.instance_get_floating_address(ctxt,
                                                         instance_ref['id'])
            if not floating_ip:
                LOG.info(_('No floating_ip found'), instance=instance_ref)
            else:
                floating_ip_ref = self.db.floating_ip_get_by_address(ctxt,
                                                              floating_ip)
                self.db.floating_ip_update(ctxt,
                                           floating_ip_ref['address'],
                                           {'host': dest})
        except exception.NotFound:
            LOG.info(_('No floating_ip found.'), instance=instance_ref)
        except Exception, e:
            LOG.error(_('Live migration: Unexpected error: cannot inherit '
                        'floating ip.\n%(e)s'), locals(),
                      instance=instance_ref)

        # Define domain at destination host, without doing it,
        # pause/suspend/terminate do not work.
        self.compute_rpcapi.post_live_migration_at_destination(ctxt,
                instance_ref, block_migration, dest)

        # No instance booting at source host, but instance dir
        # must be deleted for preparing next block migration
        if block_migration:
            self.driver.destroy(instance_ref,
                                self._legacy_nw_info(network_info))
        else:
            # self.driver.destroy() usually performs  vif unplugging
            # but we must do it explicitly here when block_migration
            # is false, as the network devices at the source must be
            # torn down
            self.driver.unplug_vifs(instance_ref,
                                    self._legacy_nw_info(network_info))

        # NOTE(tr3buchet): tear down networks on source host
        self.network_api.setup_networks_on_host(ctxt, instance_ref,
                                                self.host, teardown=True)

        LOG.info(_('Migrating instance to %(dest)s finished successfully.'),
                 locals(), instance=instance_ref)
        LOG.info(_("You may see the error \"libvirt: QEMU error: "
                   "Domain not found: no domain with matching name.\" "
                   "This error can be safely ignored."),
                 instance=instance_ref)

    def post_live_migration_at_destination(self, context, instance,
                                           block_migration=False):
        """Post operations for live migration .

        :param context: security context
        :param instance: Instance dict
        :param block_migration: if true, prepare for block migration

        """
        LOG.info(_('Post operation of migration started'),
                 instance=instance)

        # NOTE(tr3buchet): setup networks on destination host
        #                  this is called a second time because
        #                  multi_host does not create the bridge in
        #                  plug_vifs
        self.network_api.setup_networks_on_host(context, instance,
                                                         self.host)

        network_info = self._get_instance_nw_info(context, instance)
        self.driver.post_live_migration_at_destination(context, instance,
                                            self._legacy_nw_info(network_info),
                                            block_migration)
        # Restore instance state
        current_power_state = self._get_power_state(context, instance)
        self._instance_update(context,
                              instance['uuid'],
                              host=self.host,
                              power_state=current_power_state,
                              vm_state=vm_states.ACTIVE,
                              task_state=None,
                              expected_task_state=task_states.MIGRATING)

        # NOTE(vish): this is necessary to update dhcp
        self.network_api.setup_networks_on_host(context, instance, self.host)

    def _rollback_live_migration(self, context, instance_ref,
                                 dest, block_migration):
        """Recovers Instance/volume state from migrating -> running.

        :param context: security context
        :param instance_ref: nova.db.sqlalchemy.models.Instance
        :param dest:
            This method is called from live migration src host.
            This param specifies destination host.
        :param block_migration: if true, prepare for block migration

        """
        host = instance_ref['host']
        self._instance_update(context,
                              instance_ref['uuid'],
                              host=host,
                              vm_state=vm_states.ACTIVE,
                              task_state=None,
                              expected_task_state=task_states.MIGRATING)

        # NOTE(tr3buchet): setup networks on source host (really it's re-setup)
        self.network_api.setup_networks_on_host(context, instance_ref,
                                                         self.host)

        for bdm in self._get_instance_volume_bdms(context,
                                                  instance_ref['uuid']):
            volume_id = bdm['volume_id']
            volume = self.volume_api.get(context, volume_id)
            self.compute_rpcapi.remove_volume_connection(context, instance_ref,
                    volume['id'], dest)

        # Block migration needs empty image at destination host
        # before migration starts, so if any failure occurs,
        # any empty images has to be deleted.
        if block_migration:
            self.compute_rpcapi.rollback_live_migration_at_destination(context,
                    instance_ref, dest)

    def rollback_live_migration_at_destination(self, context, instance):
        """ Cleaning up image directory that is created pre_live_migration.

        :param context: security context
        :param instance: an Instance dict sent over rpc
        """
        network_info = self._get_instance_nw_info(context, instance)

        # NOTE(tr3buchet): tear down networks on destination host
        self.network_api.setup_networks_on_host(context, instance,
                                                self.host, teardown=True)

        # NOTE(vish): The mapping is passed in so the driver can disconnect
        #             from remote volumes if necessary
        block_device_info = self._get_instance_volume_block_device_info(
                            context, instance['uuid'])
        self.driver.destroy(instance, self._legacy_nw_info(network_info),
                            block_device_info)

    @manager.periodic_task
    def _heal_instance_info_cache(self, context):
        """Called periodically.  On every call, try to update the
        info_cache's network information for another instance by
        calling to the network manager.

        This is implemented by keeping a cache of uuids of instances
        that live on this host.  On each call, we pop one off of a
        list, pull the DB record, and try the call to the network API.
        If anything errors, we don't care.  It's possible the instance
        has been deleted, etc.
        """
        heal_interval = CONF.heal_instance_info_cache_interval
        if not heal_interval:
            return
        curr_time = time.time()
        if self._last_info_cache_heal + heal_interval > curr_time:
            return
        self._last_info_cache_heal = curr_time

        instance_uuids = getattr(self, '_instance_uuids_to_heal', None)
        instance = None

        while not instance or instance['host'] != self.host:
            if instance_uuids:
                try:
                    instance = self.db.instance_get_by_uuid(context,
                        instance_uuids.pop(0))
                except exception.InstanceNotFound:
                    # Instance is gone.  Try to grab another.
                    continue
            else:
                # No more in our copy of uuids.  Pull from the DB.
                db_instances = self.db.instance_get_all_by_host(
                        context, self.host)
                if not db_instances:
                    # None.. just return.
                    return
                instance = db_instances.pop(0)
                instance_uuids = [inst['uuid'] for inst in db_instances]
                self._instance_uuids_to_heal = instance_uuids

        # We have an instance now and it's ours
        try:
            # Call to network API to get instance info.. this will
            # force an update to the instance's info_cache
            self.network_api.get_instance_nw_info(context, instance)
            LOG.debug(_('Updated the info_cache for instance'),
                      instance=instance)
        except Exception:
            # We don't care about any failures
            pass

    @manager.periodic_task
    def _poll_rebooting_instances(self, context):
        if CONF.reboot_timeout > 0:
            instances = self.db.instance_get_all_hung_in_rebooting(
                context, CONF.reboot_timeout)
            self.driver.poll_rebooting_instances(CONF.reboot_timeout,
                                                 instances)

    @manager.periodic_task
    def _poll_rescued_instances(self, context):
        if CONF.rescue_timeout > 0:
            self.driver.poll_rescued_instances(CONF.rescue_timeout)

    @manager.periodic_task
    def _poll_unconfirmed_resizes(self, context):
        if CONF.resize_confirm_window > 0:
            migrations = self.db.migration_get_unconfirmed_by_dest_compute(
                    context, CONF.resize_confirm_window, self.host)

            migrations_info = dict(migration_count=len(migrations),
                    confirm_window=CONF.resize_confirm_window)

            if migrations_info["migration_count"] > 0:
                LOG.info(_("Found %(migration_count)d unconfirmed migrations "
                           "older than %(confirm_window)d seconds"),
                         migrations_info)

            def _set_migration_to_error(migration_id, reason, **kwargs):
                msg = _("Setting migration %(migration_id)s to error: "
                       "%(reason)s") % locals()
                LOG.warn(msg, **kwargs)
                self.db.migration_update(context, migration_id,
                                        {'status': 'error'})

            for migration in migrations:
                migration_id = migration['id']
                instance_uuid = migration['instance_uuid']
                LOG.info(_("Automatically confirming migration "
                           "%(migration_id)s for instance %(instance_uuid)s"),
                           locals())
                try:
                    instance = self.db.instance_get_by_uuid(context,
                                                            instance_uuid)
                except exception.InstanceNotFound:
                    reason = _("Instance %(instance_uuid)s not found")
                    _set_migration_to_error(migration_id, reason % locals())
                    continue
                if instance['vm_state'] == vm_states.ERROR:
                    reason = _("In ERROR state")
                    _set_migration_to_error(migration_id, reason % locals(),
                                            instance=instance)
                    continue
                vm_state = instance['vm_state']
                task_state = instance['task_state']
                if vm_state != vm_states.RESIZED or task_state is not None:
                    reason = _("In states %(vm_state)s/%(task_state)s, not"
                            "RESIZED/None")
                    _set_migration_to_error(migration_id, reason % locals(),
                                            instance=instance)
                    continue
                try:
                    self.compute_api.confirm_resize(context, instance)
                except Exception, e:
                    msg = _("Error auto-confirming resize: %(e)s. "
                            "Will retry later.")
                    LOG.error(msg % locals(), instance=instance)

    @manager.periodic_task
    def _instance_usage_audit(self, context):
        if CONF.instance_usage_audit:
            if not compute_utils.has_audit_been_run(context, self.host):
                begin, end = utils.last_completed_audit_period()
                instances = self.db.instance_get_active_by_window_joined(
                                                            context,
                                                            begin,
                                                            end,
                                                            host=self.host)
                num_instances = len(instances)
                errors = 0
                successes = 0
                LOG.info(_("Running instance usage audit for"
                           " host %(host)s from %(begin_time)s to "
                           "%(end_time)s. %(number_instances)s"
                           " instances.") % dict(host=self.host,
                               begin_time=begin,
                               end_time=end,
                               number_instances=num_instances))
                start_time = time.time()
                compute_utils.start_instance_usage_audit(context,
                                              begin, end,
                                              self.host, num_instances)
                for instance in instances:
                    try:
                        compute_utils.notify_usage_exists(
                            context, instance,
                            ignore_missing_network_data=False)
                        successes += 1
                    except Exception:
                        LOG.exception(_('Failed to generate usage '
                                        'audit for instance '
                                        'on host %s') % self.host,
                                      instance=instance)
                        errors += 1
                compute_utils.finish_instance_usage_audit(context,
                                              begin, end,
                                              self.host, errors,
                                              "Instance usage audit ran "
                                              "for host %s, %s instances "
                                              "in %s seconds." % (
                                              self.host,
                                              num_instances,
                                              time.time() - start_time))

    @manager.periodic_task
    def _poll_bandwidth_usage(self, context):
        prev_time, start_time = utils.last_completed_audit_period()

        curr_time = time.time()
        if (curr_time - self._last_bw_usage_poll >
                CONF.bandwidth_poll_interval):
            self._last_bw_usage_poll = curr_time
            LOG.info(_("Updating bandwidth usage cache"))

            instances = self.db.instance_get_all_by_host(context, self.host)
            try:
                bw_counters = self.driver.get_all_bw_counters(instances)
            except NotImplementedError:
                # NOTE(mdragon): Not all hypervisors have bandwidth polling
                # implemented yet.  If they don't it doesn't break anything,
                # they just don't get the info in the usage events.
                return

            refreshed = timeutils.utcnow()
            for bw_ctr in bw_counters:
                # Allow switching of greenthreads between queries.
                greenthread.sleep(0)
                bw_in = 0
                bw_out = 0
                last_ctr_in = None
                last_ctr_out = None
                usage = self.db.bw_usage_get(context,
                                             bw_ctr['uuid'],
                                             start_time,
                                             bw_ctr['mac_address'])
                if usage:
                    bw_in = usage['bw_in']
                    bw_out = usage['bw_out']
                    last_ctr_in = usage['last_ctr_in']
                    last_ctr_out = usage['last_ctr_out']
                else:
                    usage = self.db.bw_usage_get(context,
                                             bw_ctr['uuid'],
                                             prev_time,
                                             bw_ctr['mac_address'])
                    if usage:
                        last_ctr_in = usage['last_ctr_in']
                        last_ctr_out = usage['last_ctr_out']

                if last_ctr_in is not None:
                    if bw_ctr['bw_in'] < last_ctr_in:
                        # counter rollover
                        bw_in += bw_ctr['bw_in']
                    else:
                        bw_in += (bw_ctr['bw_in'] - last_ctr_in)

                if last_ctr_out is not None:
                    if bw_ctr['bw_out'] < last_ctr_out:
                        # counter rollover
                        bw_out += bw_ctr['bw_out']
                    else:
                        bw_out += (bw_ctr['bw_out'] - last_ctr_out)

                self.db.bw_usage_update(context,
                                        bw_ctr['uuid'],
                                        bw_ctr['mac_address'],
                                        start_time,
                                        bw_in,
                                        bw_out,
                                        bw_ctr['bw_in'],
                                        bw_ctr['bw_out'],
                                        last_refreshed=refreshed)

    @manager.periodic_task
    def _report_driver_status(self, context):
        curr_time = time.time()
        if curr_time - self._last_host_check > CONF.host_state_interval:
            self._last_host_check = curr_time
            LOG.info(_("Updating host status"))
            # This will grab info about the host and queue it
            # to be sent to the Schedulers.
            capabilities = self.driver.get_host_stats(refresh=True)
            for capability in (capabilities if isinstance(capabilities, list)
                               else [capabilities]):
                capability['host_ip'] = CONF.my_ip
            self.update_service_capabilities(capabilities)

    @manager.periodic_task(ticks_between_runs=10)
    def _sync_power_states(self, context):
        """Align power states between the database and the hypervisor.

        To sync power state data we make a DB call to get the number of
        virtual machines known by the hypervisor and if the number matches the
        number of virtual machines known by the database, we proceed in a lazy
        loop, one database record at a time, checking if the hypervisor has the
        same power state as is in the database.

        If the instance is not found on the hypervisor, but is in the database,
        then a stop() API will be called on the instance.
        """
        db_instances = self.db.instance_get_all_by_host(context, self.host)

        num_vm_instances = self.driver.get_num_instances()
        num_db_instances = len(db_instances)

        if num_vm_instances != num_db_instances:
            LOG.warn(_("Found %(num_db_instances)s in the database and "
                       "%(num_vm_instances)s on the hypervisor.") % locals())

        for db_instance in db_instances:
            db_power_state = db_instance['power_state']
            if db_instance['task_state'] is not None:
                LOG.info(_("During sync_power_state the instance has a "
                           "pending task. Skip."), instance=db_instance)
                continue
            # No pending tasks. Now try to figure out the real vm_power_state.
            try:
                vm_instance = self.driver.get_info(db_instance)
                vm_power_state = vm_instance['state']
            except exception.InstanceNotFound:
                vm_power_state = power_state.NOSTATE
            # Note(maoy): the above get_info call might take a long time,
            # for example, because of a broken libvirt driver.
            # We re-query the DB to get the latest instance info to minimize
            # (not eliminate) race condition.
            u = self.db.instance_get_by_uuid(context,
                                             db_instance['uuid'])
            db_power_state = u["power_state"]
            vm_state = u['vm_state']
            if self.host != u['host']:
                # on the sending end of nova-compute _sync_power_state
                # may have yielded to the greenthread performing a live
                # migration; this in turn has changed the resident-host
                # for the VM; However, the instance is still active, it
                # is just in the process of migrating to another host.
                # This implies that the compute source must relinquish
                # control to the compute destination.
                LOG.info(_("During the sync_power process the "
                           "instance has moved from "
                           "host %(src)s to host %(dst)s") %
                           {'src': self.host,
                            'dst': u['host']},
                         instance=db_instance)
                continue
            elif u['task_state'] is not None:
                # on the receiving end of nova-compute, it could happen
                # that the DB instance already report the new resident
                # but the actual VM has not showed up on the hypervisor
                # yet. In this case, let's allow the loop to continue
                # and run the state sync in a later round
                LOG.info(_("During sync_power_state the instance has a "
                           "pending task. Skip."), instance=db_instance)
                continue
            if vm_power_state != db_power_state:
                # power_state is always updated from hypervisor to db
                self._instance_update(context,
                                      db_instance['uuid'],
                                      power_state=vm_power_state)
                db_power_state = vm_power_state
            # Note(maoy): Now resolve the discrepancy between vm_state and
            # vm_power_state. We go through all possible vm_states.
            if vm_state in (vm_states.BUILDING,
                            vm_states.RESCUED,
                            vm_states.RESIZED,
                            vm_states.SUSPENDED,
                            vm_states.PAUSED,
                            vm_states.ERROR):
                # TODO(maoy): we ignore these vm_state for now.
                pass
            elif vm_state == vm_states.ACTIVE:
                # The only rational power state should be RUNNING
                if vm_power_state in (power_state.NOSTATE,
                                       power_state.SHUTDOWN,
                                       power_state.CRASHED):
                    LOG.warn(_("Instance shutdown by itself. Calling "
                               "the stop API."), instance=db_instance)
                    try:
                        # Note(maoy): here we call the API instead of
                        # brutally updating the vm_state in the database
                        # to allow all the hooks and checks to be performed.
                        self.compute_api.stop(context, db_instance)
                    except Exception:
                        # Note(maoy): there is no need to propagate the error
                        # because the same power_state will be retrieved next
                        # time and retried.
                        # For example, there might be another task scheduled.
                        LOG.exception(_("error during stop() in "
                                        "sync_power_state."),
                                      instance=db_instance)
                elif vm_power_state in (power_state.PAUSED,
                                        power_state.SUSPENDED):
                    LOG.warn(_("Instance is paused or suspended "
                               "unexpectedly. Calling "
                               "the stop API."), instance=db_instance)
                    try:
                        self.compute_api.stop(context, db_instance)
                    except Exception:
                        LOG.exception(_("error during stop() in "
                                        "sync_power_state."),
                                      instance=db_instance)
            elif vm_state == vm_states.STOPPED:
                if vm_power_state not in (power_state.NOSTATE,
                                          power_state.SHUTDOWN,
                                          power_state.CRASHED):
                    LOG.warn(_("Instance is not stopped. Calling "
                               "the stop API."), instance=db_instance)
                    try:
                        # Note(maoy): this assumes that the stop API is
                        # idempotent.
                        self.compute_api.stop(context, db_instance)
                    except Exception:
                        LOG.exception(_("error during stop() in "
                                        "sync_power_state."),
                                      instance=db_instance)
            elif vm_state in (vm_states.SOFT_DELETED,
                              vm_states.DELETED):
                if vm_power_state not in (power_state.NOSTATE,
                                          power_state.SHUTDOWN):
                    # Note(maoy): this should be taken care of periodically in
                    # _cleanup_running_deleted_instances().
                    LOG.warn(_("Instance is not (soft-)deleted."),
                             instance=db_instance)

    @manager.periodic_task
    def _reclaim_queued_deletes(self, context):
        """Reclaim instances that are queued for deletion."""
        interval = CONF.reclaim_instance_interval
        if interval <= 0:
            LOG.debug(_("CONF.reclaim_instance_interval <= 0, skipping..."))
            return

        instances = self.db.instance_get_all_by_host(context, self.host)
        for instance in instances:
            old_enough = (not instance.deleted_at or
                          timeutils.is_older_than(instance.deleted_at,
                                                  interval))
            soft_deleted = instance.vm_state == vm_states.SOFT_DELETED

            if soft_deleted and old_enough:
                bdms = self.db.block_device_mapping_get_all_by_instance(
                    context, instance['uuid'])
                LOG.info(_('Reclaiming deleted instance'), instance=instance)
                self._delete_instance(context, instance, bdms)

    @manager.periodic_task
    def update_available_resource(self, context):
        """See driver.get_available_resource()

        Periodic process that keeps that the compute host's understanding of
        resource availability and usage in sync with the underlying hypervisor.

        :param context: security context
        """
        new_resource_tracker_dict = {}
        nodenames = self.driver.get_available_nodes()
        for nodename in nodenames:
            rt = self._get_resource_tracker(nodename)
            rt.update_available_resource(context)
            new_resource_tracker_dict[nodename] = rt
        self._resource_tracker_dict = new_resource_tracker_dict

    @manager.periodic_task(
        ticks_between_runs=CONF.running_deleted_instance_poll_interval)
    def _cleanup_running_deleted_instances(self, context):
        """Cleanup any instances which are erroneously still running after
        having been deleted.

        Valid actions to take are:

            1. noop - do nothing
            2. log - log which instances are erroneously running
            3. reap - shutdown and cleanup any erroneously running instances

        The use-case for this cleanup task is: for various reasons, it may be
        possible for the database to show an instance as deleted but for that
        instance to still be running on a host machine (see bug
        https://bugs.launchpad.net/nova/+bug/911366).

        This cleanup task is a cross-hypervisor utility for finding these
        zombied instances and either logging the discrepancy (likely what you
        should do in production), or automatically reaping the instances (more
        appropriate for dev environments).
        """
        action = CONF.running_deleted_instance_action

        if action == "noop":
            return

        # NOTE(sirp): admin contexts don't ordinarily return deleted records
        with utils.temporary_mutation(context, read_deleted="yes"):
            for instance in self._running_deleted_instances(context):
                bdms = self.db.block_device_mapping_get_all_by_instance(
                    context, instance['uuid'])

                if action == "log":
                    name = instance['name']
                    LOG.warning(_("Detected instance with name label "
                                  "'%(name)s' which is marked as "
                                  "DELETED but still present on host."),
                                locals(), instance=instance)

                elif action == 'reap':
                    name = instance['name']
                    LOG.info(_("Destroying instance with name label "
                               "'%(name)s' which is marked as "
                               "DELETED but still present on host."),
                             locals(), instance=instance)
                    self._shutdown_instance(context, instance, bdms)
                    self._cleanup_volumes(context, instance['uuid'], bdms)
                else:
                    raise Exception(_("Unrecognized value '%(action)s'"
                                      " for CONF.running_deleted_"
                                      "instance_action"), locals(),
                                    instance=instance)

    def _running_deleted_instances(self, context):
        """Returns a list of instances nova thinks is deleted,
        but the hypervisor thinks is still running. This method
        should be pushed down to the virt layer for efficiency.
        """
        def deleted_instance(instance):
            timeout = CONF.running_deleted_instance_timeout
            present = instance.name in present_name_labels
            erroneously_running = instance.deleted and present
            old_enough = (not instance.deleted_at or
                          timeutils.is_older_than(instance.deleted_at,
                                                  timeout))
            if erroneously_running and old_enough:
                return True
            return False
        present_name_labels = set(self.driver.list_instances())
        instances = self.db.instance_get_all_by_host(context, self.host)
        return [i for i in instances if deleted_instance(i)]

    @contextlib.contextmanager
    def _error_out_instance_on_exception(self, context, instance_uuid,
                                        reservations=None):
        try:
            yield
        except Exception, error:
            self._quota_rollback(context, reservations)
            with excutils.save_and_reraise_exception():
                msg = _('%s. Setting instance vm_state to ERROR')
                LOG.error(msg % error, instance_uuid=instance_uuid)
                self._set_instance_error_state(context, instance_uuid)

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    def add_aggregate_host(self, context, host, subordinate_info=None,
                           aggregate=None, aggregate_id=None):
        """Notify hypervisor of change (for hypervisor pools)."""
        if not aggregate:
            aggregate = self.db.aggregate_get(context, aggregate_id)

        try:
            self.driver.add_to_aggregate(context, aggregate, host,
                                         subordinate_info=subordinate_info)
        except exception.AggregateError:
            with excutils.save_and_reraise_exception():
                self.driver.undo_aggregate_operation(context,
                                               self.db.aggregate_host_delete,
                                               aggregate['id'], host)

    @exception.wrap_exception(notifier=notifier, publisher_id=publisher_id())
    def remove_aggregate_host(self, context, host, subordinate_info=None,
                              aggregate=None, aggregate_id=None):
        """Removes a host from a physical hypervisor pool."""
        if not aggregate:
            aggregate = self.db.aggregate_get(context, aggregate_id)

        try:
            self.driver.remove_from_aggregate(context, aggregate, host,
                                              subordinate_info=subordinate_info)
        except (exception.AggregateError,
                exception.InvalidAggregateAction) as e:
            with excutils.save_and_reraise_exception():
                self.driver.undo_aggregate_operation(
                                    context, self.db.aggregate_host_add,
                                    aggregate['id'], host,
                                    isinstance(e, exception.AggregateError))

    @manager.periodic_task(
        ticks_between_runs=CONF.image_cache_manager_interval)
    def _run_image_cache_manager_pass(self, context):
        """Run a single pass of the image cache manager."""

        if not self.driver.capabilities["has_imagecache"]:
            return
        if CONF.image_cache_manager_interval == 0:
            return

        all_instances = self.db.instance_get_all(context)
        self.driver.manage_image_cache(context, all_instances)
