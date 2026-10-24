"""Exercise write-only credentials through real HTTP, Git and private files."""

import asyncio
from dataclasses import asdict
import os
from pathlib import Path
from unittest.mock import create_autospec, patch

from aiohttp.test_utils import TestClient
import pytest

from supervisor.coresys import CoreSys
from supervisor.exceptions import StoreNotFound
from supervisor.store.credentials import (
    RepositoryCredentials,
    RepositoryCredentialStore,
)
from supervisor.store.git import GitRepo
from supervisor.store.repository import Repository
from supervisor.store.utils import get_hash_from_repository

from tests.private_repository_support import (
    AUTHORIZATION,
    CREDENTIALS,
    GitHost,
    fixture_git_host as fixture_git_host,  # noqa: PLC0414 -- Re-export a shared pytest fixture.
)

_REMOVE_GIT_REPOSITORY = GitRepo.remove


@pytest.fixture(name="credential_store")
def fixture_credential_store(
    coresys: CoreSys,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> RepositoryCredentialStore:
    """Use private temporary storage and signature-checked OS disk statistics."""
    # CoreSys stubs removal to protect shared fixtures; these repositories are private copies.
    monkeypatch.setattr(GitRepo, "remove", _REMOVE_GIT_REPOSITORY)
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


async def test_rotation_with_a_damaged_clone_keeps_saved_and_active_tokens_together(
    api_client: TestClient,
    registered_repository: Repository,
    credential_store: RepositoryCredentialStore,
    git_host: GitHost,
    coresys: CoreSys,
) -> None:
    """Change credentials without reopening a broken local clone."""
    rotated = RepositoryCredentials("fixture-user", "rotated-fixture-password")
    directory = registered_repository.local_path / ".git"
    saved = directory.with_name(".git.saved")
    directory.rename(saved)
    response = await api_client.post(
        f"/store/repositories/{registered_repository.slug}/credentials",
        json={"credentials": asdict(rotated)},
    )
    saved.rename(directory)
    assert response.status == 200
    restarted = RepositoryCredentialStore(credential_store.path)
    await restarted.load()
    assert restarted.get(registered_repository.source) == rotated
    assert coresys.store.get(registered_repository.slug) is registered_repository
    git_host.credentials = rotated
    assert await registered_repository.update() is False


async def test_failed_rotation_keeps_the_saved_and_active_token(
    api_client: TestClient,
    registered_repository: Repository,
    credential_store: RepositoryCredentialStore,
) -> None:
    """A failed file replacement must leave the previous token usable."""
    before = credential_store.path.read_bytes()
    with patch(
        "atomicwrites.replace_atomic",
        autospec=True,
        spec_set=True,
        side_effect=OSError("Interrupted commit"),
    ):
        response = await api_client.post(
            f"/store/repositories/{registered_repository.slug}/credentials",
            json={
                "credentials": {"username": "fixture-user", "password": "replacement"}
            },
        )
    assert response.status == 400
    assert credential_store.get(registered_repository.source) == CREDENTIALS
    assert credential_store.path.read_bytes() == before
    restarted = RepositoryCredentialStore(credential_store.path)
    await restarted.load()
    assert restarted.get(registered_repository.source) == CREDENTIALS
    assert await registered_repository.update() is False


async def test_failed_credential_deletion_keeps_repository_removal_retryable(
    api_client: TestClient,
    registered_repository: Repository,
    credential_store: RepositoryCredentialStore,
) -> None:
    """Keep the repository until its saved credentials have been deleted."""
    endpoint = f"/store/repositories/{registered_repository.slug}"
    with patch(
        "atomicwrites.replace_atomic",
        autospec=True,
        spec_set=True,
        side_effect=OSError("Interrupted commit"),
    ):
        failed = await api_client.delete(endpoint)
    assert failed.status == 400
    assert (await api_client.get(endpoint)).status == 200
    restarted = RepositoryCredentialStore(credential_store.path)
    await restarted.load()
    assert restarted.get(registered_repository.source) == CREDENTIALS
    assert registered_repository.local_path.is_dir()

    assert (await api_client.delete(endpoint)).status == 200
    assert (await api_client.get(endpoint)).status == 404
    await restarted.load()
    assert restarted.get(registered_repository.source) is None
    assert not registered_repository.local_path.exists()


async def test_rotation_queued_after_removal_cannot_restore_credentials(
    coresys: CoreSys,
    registered_repository: Repository,
    credential_store: RepositoryCredentialStore,
) -> None:
    """Reject a queued command whose repository has already been removed."""
    removed, rotated = await asyncio.gather(
        coresys.store.remove_repository(registered_repository),
        coresys.store.set_repository_credentials(registered_repository, CREDENTIALS),
        return_exceptions=True,
    )
    assert removed is None
    assert isinstance(rotated, StoreNotFound)
    assert registered_repository.slug not in coresys.store.repositories
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
