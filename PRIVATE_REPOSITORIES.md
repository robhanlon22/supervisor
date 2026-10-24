# Private app repositories

This fork adds credentials to the Supervisor store API. It does not add a
credential form to the Home Assistant frontend. It does not change the store
refresh schedule or the app update schedule.

## Add a repository

Send `POST /store/repositories` with a JSON body:

```json
{
  "repository": "https://github.com/owner/private-apps#main",
  "credentials": {
    "username": "GITHUB_USERNAME",
    "password": "GITHUB_READ_TOKEN"
  }
}
```

Use a clean HTTPS URL. Do not put credentials in the URL. For GitHub, use a
fine-grained token with read access to repository contents. Limit it to the
required repository. Store credentials only through a trusted Supervisor API
connection. Do not put tokens in shell history, logs, or source files.

The repository URL determines its ID. Token rotation does not change that ID
or its app IDs. Public repositories still work without credentials.

## Change or remove credentials

Send `POST /store/repositories/{slug}/credentials` to set or replace credentials:

```json
{
  "credentials": {
    "username": "GITHUB_USERNAME",
    "password": "REPLACEMENT_READ_TOKEN"
  }
}
```

Send `DELETE /store/repositories/{slug}/credentials` to remove them. This keeps
the repository and its local clone. The next Git request uses the new settings.
Removal does not revoke the token at GitHub. Revoke it there when necessary.

The same routes are available under `/v2` when the existing v2 API is enabled.
The existing Supervisor access rules apply. Built-in repositories do not accept
custom credentials.

Repository metadata returns `has_credentials`. Read requests do not return the
username, password, or authorization header.

## Storage and transport

Supervisor stores credentials separately in `repository_credentials.json`.
Atomic writes keep the file private to its owner with mode `0600`. Credentials
are not encrypted on the live filesystem. Protect Supervisor storage and host
access.

The store uses Supervisor's JSON read and atomic write helpers. Repository
requests use the shared API validator with input values omitted from errors.
A missing credential file starts empty. A damaged file or failed write raises
an error and keeps the last valid in-memory credentials. The generic
`FileConfiguration` reset and error-logging paths are not used for secrets.

Git receives an authorization header through its child-process environment.
The header is scoped to the repository URL path. It is not stored in the remote
URL, Git configuration, or command arguments. Authenticated requests refuse
redirects and permit only HTTPS, including submodules. A public submodule at a
different repository path does not receive the parent's header. Submodules that
need separate private credentials are not supported by this change.

Authenticated Git errors omit remote output. Git and GitPython tracing are
disabled for authenticated commands. A privileged host process can still read
child-process memory or environment data.

Supervisor backups include credentials in the private Supervisor payload. They
do not include credentials in public backup metadata. Use a backup password to
encrypt that payload. Restore loads credentials before it clones app
repositories. Treat backups as secrets.

This change does not migrate credentials embedded in old repository URLs. Use
clean URLs and the new credential API. It does not change container-registry
authentication. A private container image still needs its own registry access.

## Test scope

The tests use synthetic credentials, disposable Git repositories, and a local
HTTPS Git server. They exercise clone, fetch, token rotation, removal, concurrent
changes, redirect refusal, submodule isolation, rejected clones, secret-safe
errors, atomic storage, and backup restoration. No household data or real token
is needed.
