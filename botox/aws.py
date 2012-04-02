# If developing this further, see the following e-buddies to collaborate as they
# also have internal/unreleased solutions and have indicated interest:
# 
# * Gavin McQuillan (gmcquillan)
# * Travis Swicegood (tswicegood)
# * Christopher Groskopf (onyxfish)


import os
import sys
import time

from boto.ec2 import regions as _ec2_regions
from boto.ec2.connection import EC2Connection as _EC2
from boto.ec2 import instance
from boto.exception import EC2ResponseError as _ResponseError
from decorator import decorator

from .utils import puts


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
        kwargs[name] = kwargs.get(name, getattr(self, name))
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
    return decorator(requires)


class AWS(object):
    def __init__(self, verbose=False, **kwargs):
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

        Other behavior-controlling options:

        * ``verbose``: Whether or not to print out detailed info about what's
          going on.
        """
        # Merge values from kwargs/shell env
        required = "access_key_id secret_access_key region".split()
        optional = "ami zone size keypair security_groups".split()
        for var in required + optional:
            env_value = os.environ.get("AWS_%s" % var.upper())
            setattr(self, var, kwargs.get(var, env_value))
        # Handle other kwargs
        self.verbose = verbose
        # Must at least have credentials + region
        missing = filter(lambda x: not getattr(self, x), required)
        if missing:
            msg = ", ".join(missing)
            raise ValueError("Missing required parameters: %s" % msg)
        # Auth creds
        boto_args = { 
            'aws_access_key_id': self.access_key_id,
            'aws_secret_access_key': self.secret_access_key,
        }
        # Obtain our default region
        regions = _ec2_regions(**boto_args)
        region = filter(lambda x: x.name == self.region, regions)[0]
        boto_args.update(region=region)
        # Get a connection to that region
        self.connection= _EC2(**boto_args)

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def get_image(self, name):
        func = PARAMETERS['ami'].get('transform')
        if func:
            name = func(name)
        return self.get_all_images([name])[0]

    def log(self, *args, **kwargs):
        """
        If ``self.verbose`` is True, acts as a proxy for ``utils.puts``.

        Otherwise, this function is a no-op.
        """
        if self.verbose:
            return puts(*args, **kwargs)

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

        This method returns a ``boto.EC2.instance.Instance`` object.
        """
        # Create
        self.log("Creating '%s' (a %s instance of %s)..." % (
            hostname, kwargs['size'], kwargs['ami']))
        instance = self._create(hostname, kwargs)
        self.log("done.\n")

        # Name
        self.log("Tagging as '%s'..." % hostname)
        try:
            instance.rename(hostname)
        # One-time retry for API errors when setting tags
        except _ResponseError:
            time.sleep(1)
            instance.rename(hostname)
        self.log("done.\n")

        # Wait for it to finish booting
        self.log("Waiting for boot: ")
        tick = 5
        while instance.state != 'running':
            self.log(".")
            time.sleep(tick)
            instance.update()
        self.log("done.\n")

        return instance

    def _create(self, hostname, kwargs):
        image = self.get_image(kwargs['ami'])
        groups = self.get_all_security_groups(kwargs['security_groups'])
        instance = image.run(
            instance_type=kwargs['size'],
            key_name=kwargs['keypair'],
            placement=kwargs['zone'],
            security_groups=groups
        ).instances[0]
        return instance
