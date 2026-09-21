import datetime
import socket
import ssl

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


class LocalLofterResolver(aiohttp.abc.AbstractResolver):
    def __init__(self, port):
        self.port = port

    async def resolve(self, host, port=0, family=socket.AF_INET):
        if host != "author.lofter.com":
            raise OSError(f"Unexpected external host in offline E2E: {host}")
        return [{"hostname": host, "host": "127.0.0.1", "port": self.port,
                 "family": socket.AF_INET, "proto": 0, "flags": 0}]

    async def close(self):
        pass


async def post_server(stack, root, handler):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "author.lofter.com")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("author.lofter.com")]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path = root / "lofter.pem"
    key_path = root / "lofter.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    server_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ssl.load_cert_chain(cert_path, key_path)
    app = web.Application()
    app.router.add_get("/post/{post_id}", handler)
    server = TestServer(app, scheme="https")
    await server.start_server(ssl=server_ssl)
    stack.push_async_callback(server.close)
    connector = aiohttp.TCPConnector(
        resolver=LocalLofterResolver(server.port),
        ssl=ssl.create_default_context(cafile=str(cert_path)),
    )
    return aiohttp.ClientSession(connector=connector)
