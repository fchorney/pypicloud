""" Store packages in S3 """
from __future__ import unicode_literals
import posixpath
import unicodedata

import boto3
import logging
from botocore.config import Config
from botocore.signers import CloudFrontSigner
from botocore.exceptions import ClientError

from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes

from datetime import datetime, timedelta
from pyramid.settings import asbool, falsey
from pyramid_duh.settings import asdict
from six.moves.urllib.parse import urlparse, quote  # pylint: disable=F0401,E0611
import six

from .object_store import ObjectStoreStorage
from pypicloud.models import Package
from pypicloud.util import parse_filename, get_settings


LOG = logging.getLogger(__name__)


class S3Storage(ObjectStoreStorage):

    """ Storage backend that uses S3 """

    test = False

    @classmethod
    def _subclass_specific_config(cls, settings, common_config):
        sse = settings.get("storage.server_side_encryption")
        if sse not in [None, "AES256", "aws:kms"]:
            LOG.warning(
                "Unrecognized value %r for 'storage.sse'. See "
                "https://boto3.readthedocs.io/en/latest/reference/services/s3.html#S3.Object.put "
                "for more details",
                sse,
            )

        return {"sse": sse}

    @classmethod
    def get_bucket(cls, bucket_name, settings):
        print("******** THIS IS THE PY ********")
        boto3.set_stream_logger('', logging.DEBUG)
        s3conn = boto3.resource('s3')
        try:
            head = s3conn.meta.client.head_bucket(Bucket='gotem')
        except Exception as e:
            print(e)
        print("******** THE PY IS DONE ********")

        config_settings = get_settings(
            settings,
            "storage.",
            region_name=str,
            signature_version=str,
            user_agent=str,
            user_agent_extra=str,
            connect_timeout=int,
            read_timeout=int,
            parameter_validation=asbool,
            max_pool_connections=int,
            proxies=asdict,
        )
        config_settings["s3"] = get_settings(
            settings,
            "storage.",
            use_accelerate_endpoint=asbool,
            payload_signing_enabled=asbool,
            addressing_style=str,
        )
        config = Config(**config_settings)

        def verify_value(val):
            """ Verify can be a boolean (False) or a string """
            s = str(val).strip().lower()
            if s in falsey:
                return False
            else:
                return str(val)

        s3_settings = get_settings(
            settings,
            "storage.",
            region_name=str,
            api_version=str,
            use_ssl=asbool,
            verify=verify_value,
            endpoint_url=str,
            aws_access_key_id=str,
            aws_secret_access_key=str,
            aws_session_token=str,
        )


        # Since boto3 session isnt used, if the credentials aren't supplied
        # in the
        # config files, then lets just grab them from the session here
        if (
            "aws_access_key" not in s3_settings
            and "aws_secret_access_key" not in s3_settings
            and "aws_session_token" not in s3_settings
        ):
            session = boto3.Session()
            credentials = session.get_credentials()
            creds = credentials.get_frozen_credentials()
            s3_settings["aws_access_key_id"] = creds.access_key
            s3_settings["aws_secret_access_key"] = creds.secret_key
            s3_settings["aws_session_token"] = creds.token

        print("********")
        print("Version 17")
        print(bucket_name)
        print(settings)
        print(config_settings)
        print(s3_settings)

        gotem = boto3.Session()
        print(gotem)
        sts = gotem.client("sts")
        cid = sts.get_caller_identity()
        print(cid)

        getem = gotem.resource("s3")
        print(getem)
        bucket = getem.Bucket(bucket_name)
        head = None
        try:
            head = getem.meta.client.head_bucket(Bucket=bucket_name)
        except Exception as e:
            print(e)

        print(bucket)
        print(head)

        getem = boto3.resource("s3", config=config, **s3_settings)
        print(getem)
        bucket = getem.Bucket(bucket_name)
        try:
            head = getem.meta.client.head_bucket(Bucket=bucket_name)
        except Exception as e:
            print(e)

        print(bucket)
        print(head)

        import os
        print("Environ: ", os.environ)
        import sys
        print("Sys: ", sys.prefix)
        import pwd
        print("User: ", pwd.getpwuid(os.getuid()))

        print("********")

        s3conn = boto3.resource(
            "s3",
            config=config,
            **s3_settings
        )

        bucket = s3conn.Bucket(bucket_name)
        try:
            head = s3conn.meta.client.head_bucket(Bucket=bucket_name)
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                LOG.info("Creating S3 bucket %s", bucket_name)
                bucket.create()
                bucket.wait_until_exists()
            else:
                if e.response["Error"]["Code"] == "301":
                    LOG.error(
                        "Bucket found in different region. Check that "
                        "the S3 bucket specified in 'storage.bucket' is "
                        "in 'storage.region_name'"
                    )
                raise
        return bucket

    @classmethod
    def package_from_object(cls, obj, factory):
        """ Create a package from a S3 object """
        filename = posixpath.basename(obj.key)
        name = obj.metadata.get("name")
        version = obj.metadata.get("version")
        summary = obj.metadata.get("summary")
        # We used to not store metadata. This is for backwards
        # compatibility
        if name is None or version is None:
            try:
                name, version = parse_filename(filename)
            except ValueError:
                LOG.warning("S3 file %s has no package name", obj.key)
                return None

        return factory(
            name, version, filename, obj.last_modified, summary, path=obj.key
        )

    def list(self, factory=Package):
        keys = self.bucket.objects.filter(Prefix=self.bucket_prefix)
        for summary in keys:
            # ObjectSummary has no metadata, so we have to fetch it.
            obj = summary.Object()
            pkg = self.package_from_object(obj, factory)
            if pkg is not None:
                yield pkg

    def _generate_url(self, package):
        """ Generate a signed url to the S3 file """
        if self.public_url:
            if self.region_name:
                return "https://s3.{0}.amazonaws.com/{1}/{2}".format(
                    self.region_name, self.bucket.name, self.get_path(package)
                )
            else:
                if "." in self.bucket.name:
                    self._log_region_warning()
                return "https://{0}.s3.amazonaws.com/{1}".format(
                    self.bucket.name, self.get_path(package)
                )
        url = self.bucket.meta.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket.name, "Key": self.get_path(package)},
            ExpiresIn=self.expire_after,
        )
        # There is a special case if your bucket has a '.' in the name. The
        # generated URL will return a 301 and the pip downloads will fail.
        # If you provide a region_name, boto should correctly generate a url in
        # the form of `s3.<region>.amazonaws.com`
        # See https://github.com/stevearc/pypicloud/issues/145
        if "." in self.bucket.name:
            pieces = urlparse(url)
            if pieces.netloc == "s3.amazonaws.com" and self.region_name is None:
                self._log_region_warning()
        return url

    def _log_region_warning(self):
        """ Spit out a warning about including region_name """
        LOG.warning(
            "Your signed S3 urls may not work! "
            "Try adding the bucket region to the config with "
            "'storage.region_name = <region>' or using a bucket "
            "without any dots ('.') in the name."
        )

    def upload(self, package, datastream):
        key = self.bucket.Object(self.get_path(package))
        kwargs = {}
        if self.sse is not None:
            kwargs["ServerSideEncryption"] = self.sse
        if self.object_acl:
            kwargs["ACL"] = self.object_acl
        if self.storage_class is not None:
            kwargs["StorageClass"] = self.storage_class
        metadata = {"name": package.name, "version": package.version}
        if package.summary:
            if isinstance(package.summary, six.text_type):
                summary = package.summary
            else:
                summary = package.summary.decode("utf-8")
            metadata["summary"] = "".join(
                c for c in unicodedata.normalize("NFKD", summary) if ord(c) < 128
            )
        key.put(Metadata=metadata, Body=datastream, **kwargs)

    def delete(self, package):
        self.bucket.delete_objects(
            Delete={"Objects": [{"Key": self.get_path(package)}]}
        )

    def check_health(self):
        try:
            self.bucket.meta.client.head_bucket(Bucket=self.bucket.name)
        except ClientError as e:
            return False, str(e)
        else:
            return True, ""


class CloudFrontS3Storage(S3Storage):

    """ Storage backend that uses S3 and CloudFront """

    def __init__(
        self, request=None, domain=None, crypto_pk=None, key_id=None, **kwargs
    ):
        super(CloudFrontS3Storage, self).__init__(request, **kwargs)
        self.domain = domain
        self.crypto_pk = crypto_pk
        self.key_id = key_id

        self.cf_signer = None
        if key_id is not None:
            self.cf_signer = CloudFrontSigner(self.key_id, self._rsa_signer)

        self.client = boto3.client("cloudfront")

    @classmethod
    def configure(cls, settings):
        kwargs = super(CloudFrontS3Storage, cls).configure(settings)
        kwargs["domain"] = settings["storage.cloud_front_domain"]
        kwargs["key_id"] = settings.get("storage.cloud_front_key_id")
        private_key = settings.get("storage.cloud_front_key_string")
        if private_key is None:
            key_file = settings.get("storage.cloud_front_key_file")
            if key_file:
                with open(key_file, "rb") as ifile:
                    private_key = ifile.read()
        else:
            private_key = private_key.encode("utf-8")
        crypto_pk = serialization.load_pem_private_key(
            private_key, password=None, backend=default_backend()
        )
        kwargs["crypto_pk"] = crypto_pk

        return kwargs

    def _rsa_signer(self, message):
        """ Generate a RSA signature for a message """
        return self.crypto_pk.sign(message, padding.PKCS1v15(), hashes.SHA1())

    def _generate_url(self, package):
        """ Get the fully-qualified CloudFront path for a package """
        path = self.get_path(package)
        url = self.domain + "/" + quote(path)

        # No key id, no signer, so we don't have to sign the URL
        if self.cf_signer is None:
            return url

        # To sign with a canned policy:
        expires = datetime.utcnow() + timedelta(seconds=self.expire_after)
        return self.cf_signer.generate_presigned_url(url, date_less_than=expires)
