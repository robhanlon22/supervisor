"""Exercise real Git requests against a synthetic HTTPS Git server."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from ipaddress import ip_address
import os
from pathlib import Path
import ssl
import subprocess
from unittest.mock import AsyncMock

from aiohttp import encode_basic_auth, web
from aiohttp.test_utils import TestServer
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import pytest
from pytest_aiohttp.plugin import AiohttpServer

from supervisor.store.credentials import RepositoryCredentials

CREDENTIALS = RepositoryCredentials("fixture-user", "fixture-password")
AUTHORIZATION = encode_basic_auth(CREDENTIALS.username, CREDENTIALS.password)


def git_command(directory: Path, *arguments: str) -> str:
    """Run the actual Git binary with disposable identity and configuration."""
    return subprocess.check_output(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-C",
            str(directory),
            *arguments,
        ],
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
        stderr=subprocess.PIPE,
        text=True,
    ).strip()


@dataclass
class GitHost:
    """Typed external server and disposable repository paths."""

    server: TestServer
    source: Path
    private: AsyncMock
    public: AsyncMock
    redirect: AsyncMock
    denied: AsyncMock
    credentials: RepositoryCredentials = CREDENTIALS

    def url(self, path: str) -> str:
        """Return an HTTPS URL on the fixture server."""
        return str(self.server.make_url(path))


@pytest.fixture(name="git_host")
async def fixture_git_host(
    tmp_path: Path,
    aiohttp_server: AiohttpServer,
    monkeypatch: pytest.MonkeyPatch,
) -> GitHost:
    """Serve actual Git CGI with a test CA and fixed route-specific checks."""
    source = tmp_path / "source"
    root = tmp_path / "repositories"
    source.mkdir()
    root.mkdir()
    await asyncio.to_thread(git_command, source, "init", "--initial-branch=main")
    (source / "marker").write_text("first")
    (source / "repository.json").write_text('{"name":"Private fixture repository"}')
    await asyncio.to_thread(git_command, source, "add", "marker", "repository.json")
    await asyncio.to_thread(git_command, source, "commit", "-m", "Fixture")
    await asyncio.to_thread(
        git_command, source, "clone", "--bare", str(source), str(root / "private.git")
    )
    await asyncio.to_thread(
        git_command, source, "clone", "--bare", str(source), str(root / "public.git")
    )
    await asyncio.to_thread(
        git_command, source, "remote", "add", "origin", str(root / "private.git")
    )

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime(2020, 1, 1, tzinfo=UTC))
        .not_valid_after(datetime(2100, 1, 1, tzinfo=UTC))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    certificate_path = tmp_path / "ca.pem"
    key_path = tmp_path / "key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate_path, key_path)
    monkeypatch.setenv("GIT_SSL_CAINFO", str(certificate_path))
    monkeypatch.delenv("GIT_SSL_NO_VERIFY", raising=False)

    async def backend(request: web.Request) -> web.Response:
        body = await request.read()
        process = await asyncio.create_subprocess_exec(
            "git",
            "http-backend",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                **os.environ,
                "GIT_PROJECT_ROOT": str(root),
                "GIT_HTTP_EXPORT_ALL": "1",
                "REQUEST_METHOD": request.method,
                "PATH_INFO": request.path,
                "QUERY_STRING": request.query_string,
                "REMOTE_ADDR": "127.0.0.1",
                "REMOTE_USER": "fixture",
                "CONTENT_TYPE": request.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(len(body)),
            },
        )
        output, _ = await process.communicate(body)
        assert process.returncode == 0
        head, content = output.split(b"\r\n\r\n", 1)
        headers = dict(line.decode().split(": ", 1) for line in head.split(b"\r\n"))
        status = int(headers.pop("Status", "200").split()[0])
        return web.Response(status=status, headers=headers, body=content)

    async def authenticated_backend(request: web.Request) -> web.Response:
        if request.headers.get("Authorization") != encode_basic_auth(
            host.credentials.username, host.credentials.password
        ):
            raise web.HTTPUnauthorized
        return await backend(request)

    async def public_backend(request: web.Request) -> web.Response:
        assert request.headers.get("Authorization") is None
        return await backend(request)

    private = AsyncMock(spec_set=backend, side_effect=authenticated_backend)
    public = AsyncMock(spec_set=backend, side_effect=public_backend)
    redirect = AsyncMock(
        spec_set=backend,
        side_effect=web.HTTPFound(location="/public.git/info/refs"),
    )
    denied = AsyncMock(
        spec_set=backend,
        side_effect=web.HTTPUnauthorized(text=CREDENTIALS.password + AUTHORIZATION),
    )

    app = web.Application()
    app.router.add_route("*", "/private.git/{tail:.*}", private)
    app.router.add_route("*", "/public.git/{tail:.*}", public)
    app.router.add_route("*", "/redirect.git/{tail:.*}", redirect)
    app.router.add_route("*", "/denied.git/{tail:.*}", denied)
    server = await aiohttp_server(app, scheme="https", ssl=context)
    host = GitHost(server, source, private, public, redirect, denied)
    return host
