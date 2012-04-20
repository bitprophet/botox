# If developing this further, see the following e-buddies to collaborate as they
# also have internal/unreleased solutions and have indicated interest:
# 
# * Gavin McQuillan (gmcquillan)
# * Travis Swicegood (tswicegood)
# * Christopher Groskopf (onyxfish)


from functools import partial
import os
import sys
import time

from boto.ec2 import regions as _ec2_regions
from boto.ec2.connection import EC2Connection as _EC2
from boto.ec2 import instance
from boto.exception import EC2ResponseError as _ResponseError
from decorator import decorator

from .utils import puts, msg


BLANK = '-'


#
# Monkeypatch boto's Instance for convenience's sake.
#
# Arguably better than the
# alternatives: returning our own Instance wrapper class in some situations --
# will only work for anything we don't proxy; or write shitty stub wrappers for
# every API call that returns Instances; etc.
#
# For now, choosing convenience over explicitness. May revisit.
#

@property
def _instance_name(self):
    return self.tags.get('Name', BLANK)

def _instance_set_name(self, name):
    return self.add_tag('Name', name)

instance.Instance.name = _instance_name
instance.Instance.rename = _instance_set_name


def _ami(x):
    if not x.startswith('ami-'):
        x = 'ami-' + x
    return x

PARAMETERS = {
    'ami': {'msg': "an AMI to use", 'transform': _ami},
    'size': {'msg': "an instance size"},
    'keypair': {'msg': "an access keypair name"},
    'zone': {'msg': "a zone ID"},
    'security_groups': {'msg': "security groups"},
    'subnet': {'msg': "VPC subnet"},
}


@decorator
def defaults(f, self, *args, **kwargs):
    """
    For ``PARAMETERS`` keys, replace None ``kwargs`` with ``self`` attr values.

    Should be applied on the top of any decorator stack so other decorators see
    the "right" kwargs.

    Will also apply transformations found in ``TRANSFORMS``.
    """
    for name, data in PARAMETERS.iteritems():
        kwargs[name] = kwargs.get(name) or getattr(self, name)
        if 'transform' in data:
            kwargs[name] = data['transform'](kwargs[name])
    return f(self, *args, **kwargs)


def requires(*params):
    """
    Raise ValueError if any ``params`` are omitted from the decorated kwargs.

    None values are considered omissions.

    Example usage on an AWS() method:

        @requires('zone', 'security_groups')
        def my_aws_method(self, custom_args, **kwargs):
            # We'll only get here if 'kwargs' contained non-None values for
            # both 'zone' and 'security_groups'.
    """
    def requires(f, self, *args, **kwargs):
        missing = filter(lambda x: kwargs.get(x) is None, params)
        if missing:
            msgs = ", ".join([PARAMETERS[x]['msg'] for x in missing])
            raise ValueError("Missing the following parameters: %s" % msgs)
        return f(self, *args, **kwargs)
    return decorator(requires)


class AWS(object):
    def __init__(self, verbose=False, config=None, **kwargs):
        """
        Set up AWS connection with the following possible parameters:

        * ``access_key_id``: AWS key ID. Will check your shell's
          ``$AWS_ACCESS_KEY_ID`` value if nothing is given at the Python level.
        * ``secret_access_key``: AWS secret key. Defaults to
          ``$AWS_SECRET_ACCESS_KEY``.
        * ``ami``: EC2 AMI ID (e.g. ``"ami-4b4ba522"``.) Default:
          ``$AWS_AMI``.
        * ``size``: EC2 size ID (e.g. ``"m1.large"``.) Default: ``$AWS_SIZE``.
        * ``region``: AWS region ID (e.g. ``"us-east-1"``.) Default:
          ``$AWS_REGION``.
        * ``zone``: AWS zone ID (e.g. ``"us-east-1d"``.). Default:
          ``$AWS_ZONE``.
        * ``keypair``: EC2 login authentication keypair name. Default:
          ``$AWS_KEYPAIR``.
        * ``security_groups``: EC2 security groups instances should default to.
          Default: ``$AWS_SECURITY_GROUPS``.
        * ``subnet``: VPC subnet ID, sans the 'subnet-' prefix. Default:
          ``$AWS_SUBNET``.

        Other behavior-controlling options:

        * ``verbose``: Whether or not to print out detailed info about what's
          going on.
        * ``config``: Custom config data (a dict, potentially nested), e.g.
          site-specific info like a subnet-ID-to-name mapping.
        """
        # Merge values from kwargs/shell env
        required = "access_key_id secret_access_key region".split()
        optional = list(set(PARAMETERS.keys()) - set(required))
        for var in required + optional:
            env_value = os.environ.get("AWS_%s" % var.upper())
            setattr(self, var, kwargs.get(var, env_value))
        # Handle other kwargs
        self.verbose = verbose
        self.config = config or {}
        # Must at least have credentials + region
        missing = filter(lambda x: not getattr(self, x), required)
        if missing:
            msg = ", ".join(missing)
            raise ValueError("Missing required parameters: %s" % msg)

    @property
    def connection(self):
        if not hasattr(self, '_connection'):
            # Auth creds
            boto_args = {
                'aws_access_key_id': self.access_key_id,
                'aws_secret_access_key': self.secret_access_key,
            }
            # Obtain our default region
            regions = _ec2_regions(**boto_args)
            region = filter(lambda x: x.name == self.region, regions)[0]
            boto_args.update(region=region)
            self._connection = _EC2(**boto_args)
        return self._connection

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def get_image(self, name):
        func = PARAMETERS['ami'].get('transform')
        if func:
            name = func(name)
        return self.get_all_images([name])[0]

    def get_security_group_id(self, name):
        """
        Take name string, give back security group ID.

        To get around VPC's API being stupid.
        """
        # Memoize entire list of groups
        if not hasattr(self, '_security_groups'):
            self._security_groups = {}
            for group in self.get_all_security_groups():
                self._security_groups[group.name] = group.id
        return self._security_groups[name]

    def get_instance_subnet_name(self, instance):
        """
        Return a human readable name for given instance's subnet, or None.

        Uses stored config mapping of subnet IDs to names.
        """
        # TODO: we have to do this here since we are monkeypatching Instance.
        # If we switch to custom Instance (sub)class then we could do it in the
        # object, provided it has access to the configuration data.
        if instance.subnet_id:
            # Account for omitted 'subnet-'
            subnet = self.config['subnets'][instance.subnet_id[7:]]
        else:
            subnet = BLANK
        return subnet

    def log(self, *args, **kwargs):
        """
        If ``self.verbose`` is True, acts as a proxy for ``utils.puts``.

        Otherwise, this function is a no-op.
        """
        if self.verbose:
            return puts(*args, **kwargs)

    @property
    def msg(self):
        return partial(msg, printer=self.log)

    @property
    def instances(self):
        """
        Generator yielding all instances in this connection's account.
        """
        for reservation in self.get_all_instances():
            for instance in reservation.instances:
                yield instance

    @defaults
    @requires('ami', 'size', 'keypair', 'security_groups', 'zone')
    def create(self, hostname, **kwargs):
        """
        Create new EC2 instance named ``hostname``.

        You may specify keyword arguments matching those of ``__init__`` (e.g.
        ``size``, ``ami``) to override any defaults given when the object was
        created, or to fill in parameters not given at initialization time.

        Additional parameters that are instance-specific:

        * ``ip``: The static private IP address for the new host.

        This method returns a ``boto.EC2.instance.Instance`` object.
        """
        # Create
        creating = "Creating '%s' (a %s instance of %s)" % (
            hostname, kwargs['size'], kwargs['ami']
        )
        with self.msg(creating):
            instance = self._create(hostname, kwargs)

        # Name
        with self.msg("Tagging as '%s'" % hostname):
            try:
                instance.rename(hostname)
            # One-time retry for API errors when setting tags
            except _ResponseError:
                time.sleep(1)
                instance.rename(hostname)

        # Wait for it to finish booting
        with self.msg("Waiting for boot: "):
            tick = 5
            while instance.state != 'running':
                self.log(".", end='')
                time.sleep(tick)
                instance.update()

        return instance

    def _create(self, hostname, kwargs):
        image = self.get_image(kwargs['ami'])
        # Security groups need special treatment to handle VPC groups
        groups = kwargs['security_groups']
        if isinstance(groups, basestring):
            groups = [groups]
        groups = map(self.get_security_group_id, groups)
        # Build kwarg dict to handle optional args
        params = dict(
            instance_type=kwargs['size'],
            key_name=kwargs['keypair'],
            placement=kwargs['zone'],
            security_group_ids=groups
        )
        # Subnet optional, if present implies VPC
        if 'subnet' in kwargs:
            params['subnet_id'] = 'subnet-' + kwargs['subnet']
        # Private IP optional
        if 'ip' in kwargs:
            params['private_ip_address'] = kwargs['ip']
        instance = image.run(**params).instances[0]
        return instance

    def get(self, arg):
        """
        Return instance object with given EC2 ID or nametag.
        """
        try:
            reservations = self.get_all_instances(filters={'tag:Name': [arg]})
            instance = reservations[0].instances[0]
        except IndexError:
            try:
                instance = self.get_all_instances([arg])[0].instances[0]
            except IndexError:
                err = "Can't find any instance with name or ID '%s'" % arg
                print >>sys.stderr, err
                sys.exit(1)
        return instance

    def get_volumes_for_instance(self, arg, device=None):
        """
        Return all EC2 Volume objects attached to ``arg`` instance name or ID.

        May specify ``device`` to limit to the (single) volume attached as that
        device.
        """
        instance = self.get(arg)
        filters = {'attachment.instance-id': instance.id}
        if device is not None:
            filters['attachment.device'] = device
        return self.get_all_volumes(filters=filters)

    def terminate(self, arg):
        """
        Terminate instance with given EC2 ID or nametag.
        """
        instance = self.get(arg)
        with self.msg("Terminating %s (%s): " % (instance.name, instance.id)):
            instance.rename("old-%s" % instance.name)
            instance.terminate()
            while instance.state != 'terminated':
                time.sleep(5)
                self.log(".", end='')
                instance.update()
