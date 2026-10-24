"""Keep private repository credentials inside the Supervisor backup payload."""

from pathlib import Path
import tarfile

import pytest

from supervisor.backups.backup import Backup
from supervisor.backups.const import BackupType
from supervisor.coresys import CoreSys
from supervisor.store.credentials import (
    RepositoryCredentials,
    RepositoryCredentialStore,
)

URL = "https://example.invalid/owner/private.git#main"
CREDENTIALS = RepositoryCredentials("fixture-user", "fixture-backup-secret")


@pytest.mark.parametrize("password", [None, "fixture-backup-password"])
async def test_credentials_round_trip_through_the_private_backup_payload(
    coresys: CoreSys,
    tmp_path: Path,
    password: str | None,
) -> None:
    """Round-trip credentials through plain and encrypted Supervisor backups."""
    store = RepositoryCredentialStore(tmp_path / "credentials.json")
    coresys.store.credentials = store
    await store.set(URL, CREDENTIALS)
    backup = Backup(coresys, tmp_path / "backup.tar", "fixture", None)
    backup.new(
        "Fixture",
        "2026-01-01T00:00:00.000000+00:00",
        BackupType.FULL,
        password=password,
    )
    async with backup.create():
        await backup.store_supervisor_config()
    await store.set(URL, None)
    async with backup.open(None):
        await backup.restore_repository_credentials()
    assert store.get(URL) == CREDENTIALS
    with tarfile.open(backup.tarfile) as archive:
        metadata = archive.extractfile("./backup.json")
        assert metadata is not None
        assert CREDENTIALS.password.encode() not in metadata.read()
