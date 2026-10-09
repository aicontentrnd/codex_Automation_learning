"""HTTPS with public-address validation, DNS pinning and verified hostname TLS.

No redirects, proxy inheritance, credentials in URLs, or private destinations.
The HTTP transport is injected in tests; production always uses this class.
"""
import http.client
import ipaddress
import json
import socket
import ssl
import time
from urllib.parse import urlsplit, urlunsplit


class DestinationError(ValueError):
    pass


def callback_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value):
        raise DestinationError('Invalid callback URL')
    try:
        parts = urlsplit(value)
        if parts.scheme != 'https' or not parts.hostname or parts.username is not None or parts.password is not None or parts.fragment or parts.port not in (None, 443):
            raise DestinationError('Callbacks must be HTTPS on port 443 without credentials or fragments')
        host = parts.hostname.encode('idna').decode('ascii').lower()
        if '%' in host or host.endswith('.'):
            raise DestinationError('Invalid callback hostname')
        display_host = f'[{host}]' if ':' in host else host
        return urlunsplit(('https', display_host, parts.path or '/', parts.query, ''))
    except (ValueError, UnicodeError) as exc:
        raise DestinationError('Invalid callback URL') from exc


def public_addresses(host: str):
    answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not answers:
        raise DestinationError('Callback hostname has no addresses')
    for family, socktype, proto, _, address in answers:
        ip = ipaddress.ip_address(address[0])
        # Reject IPv4-mapped IPv6 and transition mechanisms as well as private IPs.
        if not ip.is_global or ip.is_multicast or getattr(ip, 'ipv4_mapped', None) or getattr(ip, 'sixtofour', None) or getattr(ip, 'teredo', None):
            raise DestinationError('Callback resolved to a non-public address')
    return answers


class PinnedConnection(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, port=443, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        family, socktype, proto, _, address = self.address
        raw = socket.socket(family, socktype, proto)
        try:
            raw.settimeout(self.timeout)
            raw.connect(address)
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


class SafeHTTPS:
    def __init__(self, allowed_hosts=()):
        self.allowed_hosts = frozenset(allowed_hosts)

    def post(self, url: str, body: bytes, headers: dict) -> tuple[int, bytes]:
        parts = urlsplit(callback_url(url))
        if self.allowed_hosts and parts.hostname not in self.allowed_hosts:
            raise DestinationError('Callback hostname is not permitted')
        started = time.monotonic()
        addresses = public_addresses(parts.hostname)
        remaining = 10 - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError('Callback resolution timed out')
        connection = PinnedConnection(parts.hostname, addresses[0], remaining)
        try:
            connection.request('POST', parts.path + ('?' + parts.query if parts.query else ''), body, headers)
            response = connection.getresponse()
            chunks, total = [], 0
            while True:
                remaining = 10 - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError('Callback response timed out')
                if connection.sock:
                    connection.sock.settimeout(remaining)
                chunk = response.read1(min(4096, 65537 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > 65536:
                    raise DestinationError('Callback response too large')
                chunks.append(chunk)
            return response.status, b''.join(chunks)
        finally:
            connection.close()
