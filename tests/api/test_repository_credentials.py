"""Exercise write-only credentials through real HTTP, Git and private files."""

import asyncio
from dataclasses import asdict
import os
from pathlib import Path
from unittest.mock import create_autospec

from aiohttp.test_utils import TestClient
import pytest

from supervisor.coresys import CoreSys
from supervisor.store.credentials import (
    RepositoryCredentials,
    RepositoryCredentialStore,
)
from supervisor.store.repository import Repository
from supervisor.store.utils import get_hash_from_repository

from tests.private_repository_support import (
    AUTHORIZATION,
    CREDENTIALS,
    GitHost,
    fixture_git_host as fixture_git_host,  # noqa: PLC0414 -- Re-export a shared pytest fixture.
)


@pytest.fixture(name="credential_store")
def fixture_credential_store(
    coresys: CoreSys,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> RepositoryCredentialStore:
    """Use private temporary storage and signature-checked OS disk statistics."""
    disk = os.statvfs_result(
        (4096, 4096, 8_000_000, 6_000_000, 6_000_000, 100_000, 90_000, 90_000, 0, 255)
    )
    monkeypatch.setattr(
        os, "statvfs", create_autospec(os.statvfs, spec_set=True, return_value=disk)
    )
    store = RepositoryCredentialStore(tmp_path / "credentials.json")
    coresys.store.credentials = store
    return store


@pytest.fixture(name="registered_repository")
async def fixture_registered_repository(
    api_client: TestClient,
    coresys: CoreSys,
    credential_store: RepositoryCredentialStore,
    git_host: GitHost,
    apps_fixtures_copy: Path,
    supervisor_internet,
) -> Repository:
    """Provide a real authenticated repository for metadata and lifecycle tests."""
    url = git_host.url("/private.git")
    response = await api_client.post(
        "/store/repositories",
        json={"repository": url, "credentials": asdict(CREDENTIALS)},
    )
    assert response.status == 200
    return coresys.store.get(get_hash_from_repository(url))


@pytest.mark.usefixtures("apps_fixtures_copy", "supervisor_internet")
async def test_add_private_repository(
    api_client_with_prefix: tuple[TestClient, str],
    coresys: CoreSys,
    credential_store: RepositoryCredentialStore,
    git_host: GitHost,
) -> None:
    """Authenticate a new repository without changing its URL-derived identity."""
    client, prefix = api_client_with_prefix
    url = git_host.url("/private.git")
    response = await client.post(
        prefix + "/store/repositories",
        json={"repository": url, "credentials": asdict(CREDENTIALS)},
    )
    assert response.status == 200
    repository = coresys.store.get(get_hash_from_repository(url))
    assert repository.source == url
    assert repository.has_credentials is True
    assert credential_store.get(url) == CREDENTIALS


@pytest.mark.parametrize(
    "endpoint", ["/store", "/store/repositories", "/store/repositories/{slug}"]
)
async def test_repository_reads_do_not_expose_credentials(
    api_client: TestClient,
    registered_repository: Repository,
    endpoint: str,
) -> None:
    """Keep credentials out of every repository metadata response."""
    response = await api_client.get(endpoint.format(slug=registered_repository.slug))
    assert response.status == 200
    body = await response.text()
    assert CREDENTIALS.password not in body
    assert AUTHORIZATION not in body


async def test_rotated_credentials_are_used_by_the_next_fetch(
    api_client: TestClient,
    registered_repository: Repository,
    credential_store: RepositoryCredentialStore,
    git_host: GitHost,
    coresys: CoreSys,
) -> None:
    """Apply and persist a replacement token without replacing the repository."""
    rotated = RepositoryCredentials("fixture-user", "rotated-fixture-password")
    git_host.credentials = rotated
    response = await api_client.post(
        f"/store/repositories/{registered_repository.slug}/credentials",
        json={"credentials": asdict(rotated)},
    )
    assert response.status == 200
    assert await registered_repository.update() is False
    restarted = RepositoryCredentialStore(credential_store.path)
    await restarted.load()
    assert restarted.get(registered_repository.source) == rotated
    assert coresys.store.get(registered_repository.slug) is registered_repository


async def test_remove_credentials_keeps_the_repository(
    api_client: TestClient,
    registered_repository: Repository,
    credential_store: RepositoryCredentialStore,
    coresys: CoreSys,
) -> None:
    """Remove only private credentials, including the persisted value."""
    response = await api_client.delete(
        f"/store/repositories/{registered_repository.slug}/credentials"
    )
    assert response.status == 200
    assert registered_repository.has_credentials is False
    assert coresys.store.get(registered_repository.slug) is registered_repository
    restarted = RepositoryCredentialStore(credential_store.path)
    await restarted.load()
    assert restarted.get(registered_repository.source) is None


async def test_concurrent_rotations_keep_live_and_saved_credentials_together(
    api_client: TestClient,
    registered_repository: Repository,
    credential_store: RepositoryCredentialStore,
    git_host: GitHost,
) -> None:
    """Use the saved winner on the next real authenticated fetch."""
    endpoint = f"/store/repositories/{registered_repository.slug}/credentials"
    first, second = await asyncio.gather(
        api_client.post(endpoint, json={"credentials": asdict(CREDENTIALS)}),
        api_client.post(
            endpoint,
            json={"credentials": {"username": "fixture-user", "password": "rotated"}},
        ),
    )
    assert (first.status, second.status) == (200, 200)
    restarted = RepositoryCredentialStore(credential_store.path)
    await restarted.load()
    winner = restarted.get(registered_repository.source)
    assert winner is not None
    git_host.credentials = winner
    assert await registered_repository.update() is False


@pytest.mark.usefixtures("apps_fixtures_copy", "supervisor_internet")
async def test_failed_clone_does_not_save_credentials(
    api_client: TestClient,
    credential_store: RepositoryCredentialStore,
    git_host: GitHost,
    coresys: CoreSys,
) -> None:
    """Keep a rejected repository and its credentials out of saved state."""
    url = git_host.url("/denied.git")
    response = await api_client.post(
        "/store/repositories",
        json={"repository": url, "credentials": asdict(CREDENTIALS)},
    )
    assert response.status == 400
    assert credential_store.get(url) is None
    assert get_hash_from_repository(url) not in coresys.store.repositories
    assert CREDENTIALS.password not in await response.text()


@pytest.mark.parametrize("slug", ["core", "local"])
async def test_builtin_repositories_reject_credentials(
    api_client: TestClient,
    coresys: CoreSys,
    slug: str,
) -> None:
    """Keep built-in repository authentication under Supervisor control."""
    coresys.store.repositories[slug] = Repository.create(coresys, slug)
    endpoint = f"/store/repositories/{slug}/credentials"
    added = await api_client.post(endpoint, json={"credentials": asdict(CREDENTIALS)})
    removed = await api_client.delete(endpoint)
    assert (added.status, removed.status) == (403, 403)


@pytest.mark.parametrize(
    "body",
    [
        {
            "repository": "https://example.invalid/repo",
            "credentials": {"username": "user", "password": "supplied-secret\n"},
        },
        {
            "repository": "https://example.invalid/repo",
            "credentials": {
                "username": "user",
                "password": "supplied-secret",
                "unexpected": "field",
            },
        },
        {
            "repository": "https://user:supplied-secret@example.invalid/repo",
            "credentials": {"username": "user", "password": "supplied-secret"},
        },
        {
            "repository": "http://example.invalid/repo",
            "credentials": {"username": "user", "password": "supplied-secret"},
        },
    ],
)
async def test_invalid_private_configuration_does_not_echo_credentials(
    api_client: TestClient,
    body: dict,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Reject invalid settings before effects without exposing input secrets."""
    response = await api_client.post("/store/repositories", json=body)
    assert response.status == 400
    assert "supplied-secret" not in await response.text()
    assert "supplied-secret" not in caplog.text
