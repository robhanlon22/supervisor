"""Test private credentials through their real atomic file boundary."""

import asyncio
from pathlib import Path

import pytest
import voluptuous as vol

from supervisor.exceptions import StoreError
from supervisor.store.credentials import (
    RepositoryCredentials,
    RepositoryCredentialStore,
    parse_credentials,
    validate_credential_repository,
)

URL = "https://example.invalid/owner/private.git#main"
FIRST = RepositoryCredentials("fixture-user", "fixture-secret-one")
SECOND = RepositoryCredentials("fixture-user", "fixture-secret-two")


async def test_credentials_survive_reload(tmp_path: Path) -> None:
    """Round-trip one credential value through the real private file."""
    path = tmp_path / "credentials.json"
    store = RepositoryCredentialStore(path)
    await store.set(URL, FIRST)
    restarted = RepositoryCredentialStore(path)
    await restarted.load()
    assert restarted.get(URL) == FIRST
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_credentials_have_no_secret_representation() -> None:
    """Keep secret fields out of exception and object representations."""
    assert FIRST.password not in repr(FIRST)
    assert FIRST.username not in repr(FIRST)


async def test_concurrent_updates_preserve_both_repositories(tmp_path: Path) -> None:
    """Serialize complete file replacements without losing another secret."""
    store = RepositoryCredentialStore(tmp_path / "credentials.json")
    other = "https://example.invalid/owner/other.git"
    await asyncio.gather(store.set(URL, FIRST), store.set(other, SECOND))
    restarted = RepositoryCredentialStore(store.path)
    await restarted.load()
    assert restarted.get(URL) == FIRST
    assert restarted.get(other) == SECOND


@pytest.mark.parametrize(
    "url",
    [
        "http://example.invalid/owner/repo",
        "https://user:fixture-secret@example.invalid/owner/repo",
        "https://example.invalid/owner/repo?token=fixture-secret",
        "file:///tmp/repo",
        "https://example.invalid",
    ],
)
def test_private_repository_url_rejects_credentials_and_unsafe_transports(
    url: str,
) -> None:
    """Bind credentials only to a clean HTTPS repository URL."""
    with pytest.raises(vol.Invalid):
        validate_credential_repository(url)


@pytest.mark.parametrize(
    "value",
    [
        {"username": "bad:name", "password": "fixture-secret"},
        {"username": "fixture-user", "password": "fixture-secret\n"},
        {"username": "fixture-user", "password": "fixture-secret", "extra": "field"},
    ],
)
def test_invalid_credentials_do_not_echo_values(value: dict[str, str]) -> None:
    """Keep supplied secret values out of parsing errors."""
    with pytest.raises(vol.Invalid, match="Invalid repository credentials") as error:
        parse_credentials(value)
    assert "fixture-secret" not in str(error.value)


@pytest.mark.parametrize(
    "raw",
    [
        '{"https://example.invalid/repo":{"password":"fixture-secret"}}',
        '{"fixture-secret":',
    ],
)
async def test_corrupt_file_does_not_log_or_reset_secrets(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, raw: str
) -> None:
    """Stop a corrupt load without printing raw values or overwriting the file."""
    path = tmp_path / "credentials.json"
    store = RepositoryCredentialStore(path)
    await store.set(URL, FIRST)
    path.write_text(raw)
    with pytest.raises(StoreError, match="Cannot read repository credentials"):
        await store.load()
    assert store.get(URL) == FIRST
    assert path.read_text() == raw
    assert "fixture-secret" not in caplog.text
