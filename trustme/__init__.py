# -*- coding: utf-8 -*-

import datetime
import ssl
from base64 import urlsafe_b64encode
from tempfile import NamedTemporaryFile
from contextlib import contextmanager
import os

import ipaddress
import idna

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    PrivateFormat, NoEncryption
)
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives.serialization import Encoding

from ._version import __version__

__all__ = ["CA"]

# Python 2/3 annoyingness
try:
    unicode
except NameError:
    unicode = str

# On my laptop, making a CA + server certificate using 1024 bit keys takes ~40
# ms, and using 4096 bit keys takes ~2 seconds. We want tests to run in 40 ms,
# not 2 seconds.
_KEY_SIZE = 1024


def _name(name):
    return x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME,
                           u"trustme v{}".format(__version__)),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, name),
    ])


def random_text():
    return urlsafe_b64encode(os.urandom(12)).decode("ascii")


def _smells_like_pyopenssl(ctx):
    return getattr(ctx, "__module__", "").startswith("OpenSSL")


def _cert_builder_common(subject, issuer, public_key):
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(public_key)
        .not_valid_before(datetime.datetime(2000, 1, 1))
        # OpenSSL on Windows freaks out if you try to give it a date after
        # ~3001-01-19
        # https://github.com/pyca/cryptography/issues/3194
        .not_valid_after(datetime.datetime(3000, 1, 1))
        .serial_number(x509.random_serial_number())
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(public_key),
            critical=False,
        )
    )


def _hostname_to_x509(hostname):
    # Because we are a DWIM library for lazy slackers, we cheerfully pervert
    # the cryptography library's carefully type-safe API, and silently DTRT
    # for any of the following hostname types:
    #
    # - "example.org"
    # - "example.org"
    # - "éxamplë.org"
    # - "xn--xampl-9rat.org"
    # - "xn--xampl-9rat.org"
    # - "127.0.0.1"
    # - "::1"
    # - "10.0.0.0/8"
    # - "2001::/16"
    #
    # and wildcard variants of the hostnames.
    if not isinstance(hostname, unicode):
        raise TypeError("hostnames must be text (unicode on py2, str on py3)")

    # Have to try ip_address first, because ip_network("127.0.0.1") is
    # interpreted as being the network 127.0.0.1/32. Which I guess would be
    # fine, actually, but why risk it.
    for ip_converter in [ipaddress.ip_address, ipaddress.ip_network]:
        try:
            ip_hostname = ip_converter(hostname)
        except ValueError:
            continue
        else:
            return x509.IPAddress(ip_hostname)

    # Encode to an A-label, like cryptography wants
    if hostname.startswith("*."):
        alabel_bytes = b"*." + idna.encode(hostname[2:], uts46=True)
    else:
        alabel_bytes = idna.encode(hostname, uts46=True)
    # Then back to text, which is mandatory on cryptography 2.0 and earlier,
    # and may or may not be deprecated in cryptography 2.1.
    alabel = alabel_bytes.decode("ascii")
    return x509.DNSName(alabel)


class Blob(object):
    """A convenience wrapper for a blob of bytes.

    This type has no public constructor. They're used to provide a handy
    interface to the PEM-encoded data generated by `trustme`. For example, see
    `CA.cert_pem` or `LeafCert.private_key_and_cert_chain_pem`.

    """
    def __init__(self, data):
        self._data = data

    def bytes(self):
        """Returns the data as a `bytes` object.

        """
        return self._data

    def write_to_path(self, path, append=False):
        """Writes the data to the file at the given path.

        Args:
          path (str): The path to write to.
          append (bool): If False (the default), replace any existing file
               with the given name. If True, append to any existing file.

        """
        if append:
            mode = "ab"
        else:
            mode = "wb"
        with open(path, mode) as f:
            f.write(self._data)

    @contextmanager
    def tempfile(self, dir=None):
        """Context manager for writing data to a temporary file.

        The file is created when you enter the context manager, and
        automatically deleted when the context manager exits.

        Many libraries have annoying APIs which require that certificates be
        specified as filesystem paths, so even if you have already the data in
        memory, you have to write it out to disk and then let them read it
        back in again. If you encouter such a library, you should probably
        file a bug. But in the mean time, this context manager makes it easy
        to give them what they want.

        Example:

          Here's how to get requests to use a trustme CA (`see also
          <http://docs.python-requests.org/en/master/user/advanced/#ssl-cert-verification>`__)::

           ca = trustme.CA()
           with ca.cert_pem.tempfile() as ca_cert_path:
               requests.get("https://localhost/...", verify=ca_cert_path)

        Args:
          dir (str or None): Passed to `tempfile.NamedTemporaryFile`.

        """
        # On Windows, you can't re-open a NamedTemporaryFile that's still
        # open. Which seems like it completely defeats the purpose of having a
        # NamedTemporaryFile? Oh well...
        # https://bugs.python.org/issue14243
        f = NamedTemporaryFile(suffix=".pem", dir=dir, delete=False)
        try:
            f.write(self._data)
            f.close()
            yield f.name
        finally:
            f.close()  # in case write() raised an error
            os.unlink(f.name)


class CA(object):
    """A certificate authority.

    Attributes:
      cert_pem (`Blob`): The PEM-encoded certificate for this CA. Add this to
          your trust store to trust this CA.

    """
    def __init__(self, parent_cert=None):
        self.parent_cert = parent_cert
        self._private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=_KEY_SIZE,
            backend=default_backend()
        )

        sign_key = self._private_key
        if self.parent_cert is not None:
            sign_key = parent_cert._private_key

        name = _name(u"Testing CA #" + random_text())
        self._certificate = (
            _cert_builder_common(name, name, sign_key.public_key())
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=9), critical=True,
            )
            .sign(
                private_key=sign_key,
                algorithm=hashes.SHA256(),
                backend=default_backend(),
            )
        )

        self.cert_pem = Blob(self._certificate.public_bytes(Encoding.PEM))

    def create_child_ca(self):
        return CA(parent_cert=self)

    def issue_server_cert(self, *hostnames):
        """Issues a server certificate.

        Args:

          *hostnames: The hostname or hostnames that this certificate will be
               valid for, as a text string (``unicode`` on Python 2, ``str``
               on Python 3). That string can be in any of the following forms:

               - Regular hostname: ``example.com``
               - Wildcard hostname: ``*.example.com``
               - International Domain Name (IDN): ``café.example.com``
               - IDN in A-label form: ``xn--caf-dma.example.com``
               - IPv4 address: ``127.0.0.1``
               - IPv6 address: ``::1``
               - IPv4 network: ``10.0.0.0/8``
               - IPv6 network: ``2001::/16``

        Returns:
          LeafCert: the newly-generated server certificate.

        """
        if not hostnames:
            raise ValueError("Must specify at least one hostname")

        key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=_KEY_SIZE,
            backend=default_backend()
        )

        ski = self._certificate.extensions.get_extension_for_class(
            x509.SubjectKeyIdentifier)

        cert = (
            _cert_builder_common(
                _name(u"Testing server cert #" + random_text()),
                self._certificate.subject,
                key.public_key(),
            )
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None),
                critical=True,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(
                    ski),
                critical=False,
            )
            .add_extension(
                x509.SubjectAlternativeName(
                    [_hostname_to_x509(h) for h in hostnames]
                ),
                critical=True,
            )
            .sign(
                private_key=self._private_key,
                algorithm=hashes.SHA256(),
                backend=default_backend(),
            )
        )

        chain_to_ca = []
        ca = self
        while ca.parent_cert is not None:
            chain_to_ca.append(ca._certificate.public_bytes(Encoding.PEM))
            ca = ca.parent_cert

        return LeafCert(
                key.private_bytes(
                    Encoding.PEM,
                    PrivateFormat.TraditionalOpenSSL,
                    NoEncryption(),
                ),
                cert.public_bytes(Encoding.PEM),
                chain_to_ca,
            )

    def configure_trust(self, ctx):
        """Configure the given context object to trust certificates signed by
        this CA.

        Args:
          ctx (ssl.SSLContext or OpenSSL.SSL.Context): The SSL context to be
              modified.

        """
        if isinstance(ctx, ssl.SSLContext):
            ctx.load_verify_locations(
                cadata=self.cert_pem.bytes().decode("ascii"))
        elif _smells_like_pyopenssl(ctx):
            from OpenSSL import crypto
            cert = crypto.load_certificate(
                crypto.FILETYPE_PEM, self.cert_pem.bytes())
            store = ctx.get_cert_store()
            store.add_cert(cert)
        else:
            raise TypeError(
                "unrecognized context type {!r}"
                .format(ctx.__class__.__name__))


class LeafCert(object):
    """A server or client certificate.

    This type has no public constructor; you get one by calling
    `CA.issue_server_cert` or similar.

    Attributes:
      private_key_pem (`Blob`): The PEM-encoded private key corresponding to
          this certificate.

      cert_chain_pems (list of `Blob` objects): The zeroth entry in this list
          is the actual PEM-encoded certificate, and any entries after that
          are the rest of the certificate chain needed to reach the root CA.

      private_key_and_cert_chain_pem (`Blob`): A single `Blob` containing the
          concatenation of the PEM-encoded private key and the PEM-encoded
          cert chain.

    """
    def __init__(self, private_key_pem, server_cert_pem, chain_to_ca):
        self.private_key_pem = Blob(private_key_pem)
        self.cert_chain_pems = [
            Blob(pem) for pem in [server_cert_pem] + chain_to_ca]
        self.private_key_and_cert_chain_pem = (
            Blob(private_key_pem + server_cert_pem + b''.join(chain_to_ca)))

    def configure_cert(self, ctx):
        """Configure the given context object to present this certificate.

        Args:
          ctx (ssl.SSLContext or OpenSSL.SSL.Context): The SSL context to be
              modified.

        """
        if isinstance(ctx, ssl.SSLContext):
            # Currently need a temporary file for this, see:
            #   https://bugs.python.org/issue16487
            with self.private_key_and_cert_chain_pem.tempfile() as path:
                ctx.load_cert_chain(path)
        elif _smells_like_pyopenssl(ctx):
            from OpenSSL.crypto import (
                load_privatekey, load_certificate, FILETYPE_PEM,
            )
            key = load_privatekey(FILETYPE_PEM, self.private_key_pem.bytes())
            ctx.use_privatekey(key)
            cert = load_certificate(FILETYPE_PEM,
                                    self.cert_chain_pems[0].bytes())
            ctx.use_certificate(cert)
            for pem in self.cert_chain_pems[1:]:
                cert = load_certificate(FILETYPE_PEM, pem.bytes())
                ctx.add_extra_chain_cert(cert)
        else:
            raise TypeError(
                "unrecognized context type {!r}"
                .format(ctx.__class__.__name__))
