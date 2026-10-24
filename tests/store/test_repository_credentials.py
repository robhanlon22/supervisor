"""Test private credentials through their real atomic file boundary."""

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
import voluptuous as vol

from supervisor.exceptions import JsonFileError, StoreError
from supervisor.store.credentials import (
    RepositoryCredentials,
    RepositoryCredentialStore,
    parse_credentials,
    validate_credential_repository,
)

URL = "https://example.invalid/owner/private.git#main"
FIRST = RepositoryCredentials("fixture-user", "fixture-secret-one")
SECOND = RepositoryCredentials("fixture-user", "fixture-secret-two")


@pytest.mark.parametrize("credentials", [FIRST, SECOND])
async def test_credentials_survive_reload(
    tmp_path: Path,
    credentials: RepositoryCredentials,
) -> None:
    """Round-trip one credential value through the real private file."""
    path = tmp_path / "credentials.json"
    store = RepositoryCredentialStore(path)
    await store.set(URL, credentials)
    restarted = RepositoryCredentialStore(path)
    await restarted.load()
    assert restarted.get(URL) == credentials


async def test_credential_file_is_owner_only(tmp_path: Path) -> None:
    """Create the credential file without group or other access."""
    store = RepositoryCredentialStore(tmp_path / "credentials.json")
    await store.set(URL, FIRST)
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


async def test_credential_removal_survives_restart(tmp_path: Path) -> None:
    """Delete only the selected repository's credentials."""
    store = RepositoryCredentialStore(tmp_path / "credentials.json")
    await store.set(URL, FIRST)
    await store.set(URL, None)
    restarted = RepositoryCredentialStore(store.path)
    await restarted.load()
    assert restarted.get(URL) is None
    assert "fixture-secret" not in store.path.read_text()


async def test_failed_write_keeps_the_previous_credentials(tmp_path: Path) -> None:
    """A failed atomic replacement cannot publish new in-memory credentials."""
    store = RepositoryCredentialStore(tmp_path / "credentials.json")
    await store.set(URL, FIRST)
    before = store.path.read_bytes()
    with (
        patch(
            "atomicwrites.replace_atomic",
            autospec=True,
            spec_set=True,
            side_effect=OSError("Interrupted commit"),
        ),
        pytest.raises(JsonFileError),
    ):
        await store.set(URL, SECOND)
    assert store.get(URL) == FIRST
    assert store.path.read_bytes() == before


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


async def test_corrupt_file_does_not_log_or_reset_secrets(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Stop a corrupt load without printing raw values or overwriting the file."""
    path = tmp_path / "credentials.json"
    raw = '{"https://example.invalid/repo":{"password":"fixture-secret"}}'
    path.write_text(raw)
    store = RepositoryCredentialStore(path)
    with pytest.raises(StoreError, match="Cannot read repository credentials"):
        await store.load()
    assert path.read_text() == raw
    assert "fixture-secret" not in caplog.text
