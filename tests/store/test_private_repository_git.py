"""Exercise real Git requests against a synthetic HTTPS Git server."""

import asyncio
import logging
from pathlib import Path

from aiohttp import web
import git
import pytest

from supervisor.coresys import CoreSys
from supervisor.exceptions import StoreGitCloneError, StoreGitError
from supervisor.store.credentials import RepositoryCredentialStore
from supervisor.store.git import GitRepo
from supervisor.utils import get_message_from_exception_chain

from tests.private_repository_support import (
    AUTHORIZATION,
    CREDENTIALS,
    GitHost,
    fixture_git_host as fixture_git_host,  # noqa: PLC0414 -- Re-export a shared pytest fixture.
    git_command,
)


@pytest.fixture(name="private_git")
async def fixture_private_git(
    git_host: GitHost,
    coresys: CoreSys,
    tmp_path: Path,
) -> GitRepo:
    """Bind real Git operations to a real private credential file."""
    coresys.store.credentials = RepositoryCredentialStore(tmp_path / "credentials.json")
    url = git_host.url("/private.git")
    await coresys.store.credentials.set(url, CREDENTIALS)
    return GitRepo(coresys, tmp_path / "clone", url)


@pytest.fixture(name="cloned_private_git")
async def fixture_cloned_private_git(private_git: GitRepo) -> GitRepo:
    """Provide an authenticated clone for fetch and credential lifecycle tests."""
    await private_git.clone.__wrapped__(private_git)
    return private_git


async def test_private_clone_keeps_authentication_ephemeral(
    private_git: GitRepo,
    git_host: GitHost,
) -> None:
    """Clone with credentials but retain only the clean remote URL."""
    await private_git.clone.__wrapped__(private_git)
    assert (private_git.path / "marker").read_text() == "first"
    assert private_git.repo is not None
    assert private_git.repo.remotes.origin.url == git_host.url("/private.git")
    assert "GIT_CONFIG_VALUE_0" not in private_git.repo.git.environment()
    config = (private_git.path / ".git/config").read_text()
    assert CREDENTIALS.password not in config
    assert AUTHORIZATION not in config


async def test_private_fetch_applies_the_next_commit(
    cloned_private_git: GitRepo,
    git_host: GitHost,
) -> None:
    """Fetch a real authenticated update into the existing clone."""
    (git_host.source / "marker").write_text("second")
    await asyncio.to_thread(git_command, git_host.source, "commit", "-am", "Second")
    await asyncio.to_thread(git_command, git_host.source, "push", "origin", "main")
    assert await cloned_private_git.pull.__wrapped__(cloned_private_git) is True
    assert (cloned_private_git.path / "marker").read_text() == "second"


async def test_removed_credentials_are_not_reused_after_clone(
    cloned_private_git: GitRepo,
    git_host: GitHost,
) -> None:
    """Clearing credentials also clears GitPython's retained clone environment."""
    git_host.private.reset_mock()
    await cloned_private_git.set_credentials(None)
    with pytest.raises(StoreGitError):
        await cloned_private_git.pull.__wrapped__(cloned_private_git)
    git_host.private.assert_awaited_once()
    assert git_host.private.await_args is not None
    request: web.Request = git_host.private.await_args.args[0]
    assert request.headers.get("Authorization") is None
    assert request.method == "GET"
    assert str(request.rel_url) == "/private.git/info/refs?service=git-upload-pack"


async def test_private_credentials_are_not_forwarded_to_a_public_submodule(
    private_git: GitRepo,
    git_host: GitHost,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Keep parent credentials out of public submodule requests and clone logs."""
    caplog.set_level(logging.DEBUG)
    submodule_path = CREDENTIALS.password
    revision = await asyncio.to_thread(
        git_command, git_host.source, "rev-parse", "HEAD"
    )
    (git_host.source / ".gitmodules").write_text(
        '[submodule "child"]\n'
        f"path = {submodule_path}\n"
        f"url = {git_host.url('/public.git')}\n"
    )
    await asyncio.to_thread(git_command, git_host.source, "add", ".gitmodules")
    await asyncio.to_thread(
        git_command,
        git_host.source,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{revision},{submodule_path}",
    )
    await asyncio.to_thread(git_command, git_host.source, "commit", "-m", "Submodule")
    await asyncio.to_thread(git_command, git_host.source, "push", "origin", "main")
    await private_git.clone.__wrapped__(private_git)
    assert (private_git.path / submodule_path / "marker").read_text() == "first"
    git_host.public.assert_awaited()
    assert CREDENTIALS.password not in caplog.text
    assert AUTHORIZATION not in caplog.text


async def test_authenticated_redirect_does_not_reach_another_repository(
    git_host: GitHost,
    coresys: CoreSys,
    tmp_path: Path,
) -> None:
    """Refuse redirects instead of forwarding the authorization header."""
    coresys.store.credentials = RepositoryCredentialStore(tmp_path / "credentials.json")
    url = git_host.url("/redirect.git")
    await coresys.store.credentials.set(url, CREDENTIALS)
    repo = GitRepo(coresys, tmp_path / "clone", url)
    with pytest.raises(StoreGitCloneError):
        await repo.clone.__wrapped__(repo)
    git_host.redirect.assert_awaited_once()
    assert git_host.redirect.await_args is not None
    request: web.Request = git_host.redirect.await_args.args[0]
    assert request.headers.get("Authorization") == AUTHORIZATION
    assert request.method == "GET"
    assert str(request.rel_url) == "/redirect.git/info/refs?service=git-upload-pack"
    git_host.public.assert_not_awaited()


async def test_authenticated_errors_do_not_echo_remote_output_or_full_trace(
    git_host: GitHost,
    coresys: CoreSys,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep server-echoed secrets out of logs even with full GitPython tracing."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(git.Git, "GIT_PYTHON_TRACE", "full")
    coresys.store.credentials = RepositoryCredentialStore(tmp_path / "credentials.json")
    url = git_host.url("/denied.git")
    await coresys.store.credentials.set(url, CREDENTIALS)
    repo = GitRepo(coresys, tmp_path / "clone", url)
    with pytest.raises(StoreGitCloneError) as error:
        await repo.clone.__wrapped__(repo)
    git_host.denied.assert_awaited_once()
    assert git_host.denied.await_args is not None
    request: web.Request = git_host.denied.await_args.args[0]
    assert request.headers.get("Authorization") == AUTHORIZATION
    assert (
        get_message_from_exception_chain(error.value)
        == "Authenticated Git operation failed"
    )
    assert CREDENTIALS.password not in caplog.text
    assert AUTHORIZATION not in caplog.text
