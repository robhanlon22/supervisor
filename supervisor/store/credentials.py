"""Private HTTPS repository credentials, separate from repository identity."""

import asyncio
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import voluptuous as vol

from ..const import ATTR_PASSWORD, ATTR_USERNAME
from ..exceptions import JsonFileError, StoreError
from ..utils.json import read_json_file, write_json_file
from ..validate import RE_REPOSITORY

_SECRET = vol.All(str, vol.Length(min=1, max=4096), vol.Match(r"^[^\x00\r\n]+\Z"))
SCHEMA_CREDENTIALS = vol.Schema(
    {
        vol.Required(ATTR_USERNAME): vol.All(_SECRET, vol.Match(r"^[^:]+\Z")),
        vol.Required(ATTR_PASSWORD): _SECRET,
    }
)


@dataclass(frozen=True, repr=False)
class RepositoryCredentials:
    """Keep credentials out of object and exception representations."""

    username: str
    password: str


def parse_credentials(value: Any) -> RepositoryCredentials:
    """Parse credentials without putting their values in errors."""
    try:
        return RepositoryCredentials(**SCHEMA_CREDENTIALS(value))
    except vol.Invalid:
        raise vol.Invalid("Invalid repository credentials") from None


def validate_credential_repository(repository: str) -> str:
    """Require a clean HTTPS URL for a credential-bound repository."""
    match = RE_REPOSITORY.fullmatch(repository)
    try:
        url = urlsplit(match["url"] if match else "")
        if (
            url.scheme != "https"
            or not url.hostname
            or not url.path.strip("/")
            or url.username is not None
            or url.query
        ):
            raise ValueError
        _ = url.port
    except ValueError:
        raise vol.Invalid("Repository credentials require a clean HTTPS URL") from None
    return repository


SCHEMA_CREDENTIAL_STORE = vol.Schema(
    {validate_credential_repository: parse_credentials}
)


class RepositoryCredentialStore:
    """Atomically persist credentials with owner-only file permissions."""

    def __init__(self, path: Path) -> None:
        """Initialize an empty credential store."""
        self.path = path
        self._credentials: dict[str, RepositoryCredentials] = {}
        self._lock = asyncio.Lock()

    def get(self, repository: str) -> RepositoryCredentials | None:
        """Return credentials bound to the full clean repository URL."""
        return self._credentials.get(repository)

    def export_credentials(self) -> dict[str, dict[str, str]]:
        """Export secrets only for the private Supervisor backup payload."""
        return {url: asdict(value) for url, value in self._credentials.items()}

    async def restore_credentials(self, data: Any) -> None:
        """Parse and merge a private backup before repository restoration."""
        try:
            restored = SCHEMA_CREDENTIAL_STORE(data)
        except vol.Invalid:
            raise StoreError("Invalid repository credentials backup") from None
        async with self._lock:
            await self._save(self._credentials | restored)

    async def load(self) -> None:
        """Load private values without the generic configuration error logger."""

        async with self._lock:
            try:
                data = await asyncio.get_running_loop().run_in_executor(
                    None, partial(read_json_file, self.path, default={})
                )
                self._credentials = SCHEMA_CREDENTIAL_STORE(data)
            except JsonFileError, vol.Invalid:
                raise StoreError("Cannot read repository credentials") from None

    async def set(
        self, repository: str, credentials: RepositoryCredentials | None
    ) -> None:
        """Publish a new value only after its atomic file write succeeds."""
        validate_credential_repository(repository)
        async with self._lock:
            updated = self._credentials.copy()
            if credentials is None:
                updated.pop(repository, None)
            else:
                updated[repository] = credentials
            await self._save(updated)

    async def _save(self, updated: dict[str, RepositoryCredentials]) -> None:
        """Publish credentials after Supervisor's atomic writer succeeds."""
        await asyncio.get_running_loop().run_in_executor(
            None,
            write_json_file,
            self.path,
            {url: asdict(value) for url, value in updated.items()},
        )
        self._credentials = updated
