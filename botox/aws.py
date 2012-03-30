# If developing this further, see the following e-buddies to collaborate as they
# also have internal/unreleased solutions and have indicated interest:
# 
# * Gavin McQuillan (gmcquillan)
# * Travis Swicegood (tswicegood)
# * Christopher Groskopf (onyxfish)


import os
import pprint
import sys
import time

from boto.ec2 import regions as _ec2_regions
from boto.ec2.connection import EC2Connection as _EC2
from boto.ec2 import instance
from boto.exception import EC2ResponseError as _ResponseError
from prettytable import PrettyTable as _Table

from .utils import puts


BLANK = '-'


# Monkeypatching
@property
def _instance_name(self):
    return self.tags.get('Name', BLANK)

instance.Instance.name = _instance_name



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
        return self.get_all_images([name])[0]

    def log(self, *args, **kwargs):
        """
        If ``self.verbose`` is True, acts as a proxy for ``utils.puts``.

        Otherwise, this function is a no-op.
        """
        if self.verbose:
            return puts(*args, **kwargs)

    def create(self, hostname, **kwargs):
        """
        Create new EC2 instance named ``hostname``.

        Available keyword arguments follow. All values will default to the
        attributes set when initializing this AWS object, e.g. if ``size`` is
        omitted, it will fall back to ``self.size``.

        * ``size``
        * ``ami``
        * ``keypair``
        * ``zone``
        * ``security_groups``

        This method returns a ``boto.EC2.instance.Instance`` object.

        Example usage::

            AWS(credentials).create(
                hostname='web1.example.com',
                size='m1.large',
                ami='abc123'
            )
        """
        # Parameter handling
        params = {
            'ami': "an AMI to use",
            'size': "an instance size",
            'keypair': "an access keypair name",
            'zone': "a zone ID",
            'security_groups': "security groups",
        }
        for var, msg in params.iteritems():
            kwargs[var] = kwargs.get(var, getattr(self, var))
        missing = filter(lambda x: not kwargs[x], params.keys())
        if missing:
            msgs = ", ".join([params[x] for x in missing])
            raise ValueError("Missing the following parameters: %s" % msgs) 

        # Create instance
        self.log("Creating '%s' (a %s instance of %s)..." % (
            hostname, kwargs['size'], kwargs['ami']))
        image = self.get_image(kwargs['ami'])
        groups = self.get_all_security_groups(kwargs['security_groups'])
        instance = image.run(
            instance_type=kwargs['size'],
            key_name=kwargs['keypair'],
            placement=kwargs['zone'],
            security_groups=groups
        ).instances[0]
        self.log("done.\n")

        # Name it
        self.log("Tagging as '%s'..." % hostname)
        try:
            instance.add_tag('Name', hostname)
        except _ResponseError:
            time.sleep(1)
            instance.add_tag('Name', hostname)
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
