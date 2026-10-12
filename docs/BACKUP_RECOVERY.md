# Backup and recovery operations

## Status and scope

`scripts/backup_recovery.py` is standalone host-side tooling for the initial
single-VPS Compose deployment. It deliberately lives outside the
`backend/iluvtrade` runtime package: its Docker/Restic subprocess calls and
host-side staging file operations are not application request-path behavior.
It is **not scheduled, provisioned, or verified against the live deployment**.
It has not performed a real Docker capture, Restic upload, or restore.
Repository tests mock Docker and Restic.

The design uses the existing PostgreSQL service and file-backed storage. It
does not move data to object storage. It uses Restic's client-side encryption
and a Backblaze B2 repository; no provider credentials, repository name, local
path, or application key are stored in this repository.

The script coordinates every service declared in the selected Compose project:
it keeps the configured database service running and gracefully stops every
other running service. It refuses Compose orphan containers and checks that no
other PostgreSQL client sessions remain before and after capture. In the
current Compose file those services are `db` and `api`; future worker or
application services are treated as writers too.

This does **not** discover an application process outside that Compose project,
a process with direct access to the storage volume, or a client that connects
after the database-session checks. Before scheduling, ensure the database and
storage volume are private to this project, stop all host/external writers, and
prevent external database connections for the maintenance window. The current
Compose file does not publish the database port. Do not use this workflow if
another deployment or operator can write to either data store during capture.

## Recovery objectives

| Objective | Initial target | Status |
|---|---|---|
| Recovery point objective (RPO) | 24 hours | Proposed only; not measured or guaranteed |
| Recovery time objective (RTO) | 8 hours | Proposed only; not measured or guaranteed |

The target assumes a successful nightly backup. A missed or failed run makes
the actual recovery point older; monitoring must alert rather than infer success
from the schedule.

## What is captured

Each coordinated local recovery bundle contains:

- A PostgreSQL custom-format `pg_dump` made while application services are
  stopped. PostgreSQL remains running. `pg_dump` provides a consistent
  database snapshot.
- The entire configured storage root copied from its verified persistent
  Compose mount after the writers stop. This includes raw uploads, canonical
  and rejected dataset files, backtest result artifacts, and other storage
  objects.
- A manifest with UTC capture time, application package version and explicitly
  configured deployed Git commit, non-secret deployment UUID, Alembic revision,
  Compose-file SHA-256, each bundle-file checksum, and a JSONL inventory of
  storage paths, byte sizes, and SHA-256 hashes.

The manifest does not contain credentials, database URLs, application key
material, Restic passwords, B2 credentials, or `.env` contents. The Compose
file hash identifies the checked-in deployment definition, not the secret
values injected at deployment.

The database and storage capture are coordinated by a short maintenance
window: all Compose services except PostgreSQL stop; unexpected PostgreSQL
clients cause an abort; then the dump and storage copy are captured. Compose
services that were running are started again in a `finally` path, including
after a capture error, and their running state is checked. If a service cannot
be restarted, the tool fails and reports that manual recovery is required.
Offsite upload and retention happen **after** the writers have resumed.

## Host configuration

Install the Restic client and a Docker Compose-compatible Docker CLI on the
future host. Run the module with the application Python environment. The local
staging directory must be on a filesystem with enough free space for a full
database dump, full storage archive, and temporary copy; it must be outside the
repository and outside the application data volumes. Local database dumps and
storage archives are plaintext until Restic finishes the upload. Restrictive
permissions protect against other local accounts but are **not encryption**.
The staging filesystem must be encrypted at rest by the host before enabling
backups. Independently verify that property from the host/storage configuration;
the script cannot reliably detect filesystem, block-device, or provider-level
encryption across deployment environments.

Set `ILUVTRADE_BACKUP_STAGING_ENCRYPTED=1` only after that independent
verification. This required setting is an operator acknowledgement, not
cryptographic proof. Without it, configuration validation fails closed and
neither capture nor pending-bundle upload can start. On upload or retention
failure, the complete bundle is intentionally retained in that encrypted
staging filesystem for recovery/retry, with restrictive directory/file
permissions; do not copy it to unencrypted media. If the host cannot provide
encrypted staging, do not run the workflow until a separately reviewed
encryption-at-rest mechanism is provided.

Supply configuration through a host-managed environment file loaded by the
scheduler. Keep that file root-owned (or owned by the dedicated backup account)
with mode `0600`; do not commit it or copy it into the backup bundle. The
Restic password file must be a regular file with mode `0600` or stricter. The
tool rejects missing or insecure configuration without displaying values.

Required variables:

| Variable | Purpose |
|---|---|
| `RESTIC_REPOSITORY` | B2 repository identifier in the form `b2:<bucket>:<prefix>`; use a repository dedicated to this deployment |
| `RESTIC_PASSWORD_FILE` | Path to the separately protected Restic password file |
| `B2_ACCOUNT_ID`, `B2_ACCOUNT_KEY` | Restricted B2 application-key credentials, supplied only to the process environment |
| `ILUVTRADE_BACKUP_STAGING_DIR` | Absolute host path for private recovery bundles and the overlap lock; outside the project and data volumes |
| `ILUVTRADE_BACKUP_STAGING_ENCRYPTED` | Must be `1` after independently confirming that the staging filesystem is encrypted at rest; acknowledgement only, not automatic detection |
| `ILUVTRADE_BACKUP_TAG` | Explicit tag unique to this deployment; retention applies only to snapshots carrying this tag |
| `ILUVTRADE_BACKUP_DEPLOYMENT_ID` | Canonical UUID generated once for this deployment; non-secret ownership tag, never reuse or copy to another deployment |
| `ILUVTRADE_BACKUP_APP_COMMIT` | Full Git commit hash of the application version actually deployed |

Optional variables:

| Variable | Default | Purpose |
|---|---|---|
| `ILUVTRADE_BACKUP_PROJECT_DIR` | Repository root containing this module | Compose project directory |
| `ILUVTRADE_BACKUP_COMPOSE_FILE` | `<project-dir>/docker-compose.yml` | Compose file to inspect and operate |
| `ILUVTRADE_BACKUP_DB_SERVICE` | `db` | Compose PostgreSQL service |
| `ILUVTRADE_BACKUP_STORAGE_SERVICE` | `api` | Service with the persistent storage mount |
| `ILUVTRADE_BACKUP_STORAGE_PATH` | `/var/lib/iluvtrade/storage` | Storage mount path inside that service |
| `ILUVTRADE_BACKUP_STOP_TIMEOUT` | `60` | Graceful Compose stop timeout in seconds, from 5 to 600 |
| `ILUVTRADE_BACKUP_DOCKER`, `ILUVTRADE_BACKUP_RESTIC` | `docker`, `restic` | Executable names or paths |

Restic reads its repository password from the password file and uses B2's
standard environment variables. Neither credentials nor the repository name
are passed as shell-expanded command text or printed by the script. Docker
daemon access is highly privileged; grant it only to the trusted host service
that runs the backup.

Initialize the Restic repository once, under the protected host configuration,
using the installed Restic client. Do not let a scheduled job silently
initialize an absent or mistyped repository.

## Running a backup

Invoke the script by its repository path (preferably an absolute path); it does
not need to be installed as an application module or run from a particular
working directory. The default invocation is a configuration-only dry run. It
does not call Docker, PostgreSQL, or Restic:

```text
<python> /path/to/iluvtrade/scripts/backup_recovery.py
```

An actual coordinated capture and upload requires the explicit `--execute`
action:

```text
<python> /path/to/iluvtrade/scripts/backup_recovery.py --execute
```

The process takes an exclusive `flock` in the private staging directory. It
validates Restic repository access before downtime, discovers Compose services
and running containers, verifies the storage mount, stops all currently
running non-database services, checks for remaining database clients, captures
the dump and storage, writes checksums/manifest, then resumes the services.
Restic encrypts the completed bundle client-side before upload. Each snapshot
gets both the configured descriptive tag and a tag derived from the unique
deployment UUID. Before retention, the tool queries only the UUID tag and
refuses to prune if any returned snapshot lacks the expected deployment and
descriptive tags. The `forget` command is scoped only to the UUID tag, so reuse
of a descriptive tag by another deployment cannot target its snapshots. The
tool confirms the new snapshot exists, applies retention, then confirms the new
snapshot still exists before reporting success.

Retention is 7 daily, 4 weekly, and 6 monthly snapshots, plus a floor of the
latest 2 snapshots. Retention groups by tags because each local recovery bundle
has a unique path. The descriptive tag is required; the unique deployment UUID
tag is the actual retention boundary. Generate a new UUID for each deployment,
preserve it for that deployment's lifetime, and never copy it to another
deployment. Use a dedicated repository as an additional isolation measure. No
retention command runs unless the just-uploaded snapshot has first been found
with the expected ownership tags.

If upload or retention fails, the local bundle is retained. After correcting
the external failure, retry that exact bundle explicitly:

```text
<python> /path/to/iluvtrade/scripts/backup_recovery.py --upload-bundle <absolute-bundle-path>
```

That operation verifies local checksums and does not stop Compose services. A
bundle is deleted from staging only after upload, retention, and final snapshot
verification succeed. Keep enough local space for a failed-upload bundle and
alert promptly so it is not mistaken for an offsite backup.

## Nightly schedule and alerting

Configure a host scheduler (for example, a systemd oneshot service and timer)
to run `--execute` nightly, initially at 02:00 in the host's configured
timezone, with persistent catch-up enabled after host downtime. Use the
protected environment file as the service's environment source. Do not put
credentials in a unit file or command line. Confirm the chosen local staging
filesystem and B2 permissions before enabling the timer.

For practical failure/missed-run alerting:

1. Configure the host service manager to send service failures to an
   operator-provided `OnFailure` notification unit or monitoring agent.
2. Have an **independent** monitor check the age of the newest Restic snapshot
   carrying this deployment's tag at least hourly. Alert if it exceeds the
   24-hour proposed RPO threshold, and alert on failed authentication,
   repository access, retention, or local disk-space checks.
3. Alert on a retained local recovery bundle, since it signals an incomplete
   offsite workflow.
4. Monitor the scheduler's last successful completion, not just that the timer
   is enabled.

This repository installs none of those host services or notifications.
Monitoring is not active until external host configuration and an independent
alert destination have both been installed and tested. Periodically test the
alert path by a controlled failure in a non-production rehearsal.

## Encryption-key recovery

Recovery needs more than the Restic snapshot:

- `ILUVTRADE_SECRET_KEY` must match the key used by the database snapshot to
  decrypt broker credentials and MFA secrets.
- Every retired application key still needed by data in retained snapshots
  must remain available. Keep old keys until data has been re-encrypted **and**
  all backups that may contain old ciphertext have expired.
- The Restic repository password is required to decrypt repository contents.
- The restricted B2 application key is required to access the offsite
  repository.

Store these materials in an independently protected secrets manager or
equivalent offline escrow, with access control and recovery authorization
separate from the B2 backup location and the VPS. Never place raw application
keys, `.env`, Restic passwords, B2 credentials, or their unencrypted copies in
the backup repository. Record non-secret secret-manager version references in
the operator's protected deployment inventory so the key version paired with
each recovery point can be selected.

Before relying on recovery, perform an authorized key-retrieval rehearsal:
retrieve the required versions through the approved access path, use them only
in the isolated restore environment, verify encrypted values can be decrypted,
and verify access is audited. Do not include secret values in test output or
the rehearsal report. No automated application-key rotation is part of this
tool.

## Isolated restore rehearsal

Run a rehearsal periodically and after changes to application, PostgreSQL,
storage, encryption, or backup configuration. Use a disposable host or
isolated environment with no production database, storage, broker, payment, or
other service access. Keep live trading disabled and outbound network access
blocked except for the controlled backup read. Do not mount production
volumes.

1. Select an exact Restic snapshot ID and retrieve it into a new private scratch
   directory. Do not restore over production or use an implicit `latest` in a
   real recovery:

   ```text
   restic restore <snapshot-id> --target <new-private-scratch-directory>
   ```

2. Locate the restored bundle by its `recovery-*` directory and run offline
   verification before extracting or restoring:

   ```text
   <python> /path/to/iluvtrade/scripts/backup_recovery.py --verify-bundle <restored-bundle>
   ```

   This validates the database-dump checksum, storage archive checksum,
   inventory checksum, and every stored file's byte count and SHA-256 against
   the inventory.

3. Create a new disposable PostgreSQL database on an isolated test instance.
   Use protected connection configuration (for example, a restricted
   `PGPASSFILE`); never put a password in a command argument. Restore only to
   that disposable database:

   ```text
   pg_restore --no-owner --no-acl --dbname=<disposable-database-name> <bundle>/database.dump
   ```

4. Compare its Alembic revision with `<bundle>/manifest.json`'s
   `alembic_revision`. Query the restored database using a protected connection
   environment. Export file references as two tab-separated columns (`path`,
   `content_hash`); include paths with null/legacy hashes using an empty second
   column:

   ```sql
   SELECT raw_path, content_hash FROM data_sources
   UNION ALL
   SELECT canonical_path, canonical_hash FROM dataset_versions
     WHERE canonical_path IS NOT NULL
   UNION ALL
   SELECT artifact_path, artifact_sha256 FROM backtest_runs
     WHERE artifact_path IS NOT NULL;
   ```

   Write the query output to a scratch TSV, then compare every DB path and any
   DB-recorded digest with the bundle's storage inventory:

   ```text
   <python> /path/to/iluvtrade/scripts/backup_recovery.py --verify-references \
     <bundle>/storage-checksums.jsonl <scratch>/database-references.tsv
   ```

   A reported legacy reference without an application-recorded digest is
   counted; its actual bytes are still checked against the bundle inventory.
   Missing paths or digest disagreements fail verification.

5. Extract `storage.tar` only into a new empty disposable storage root. Start
   the matching application release in the isolated environment, providing
   recovered application-key versions through the protected secret mechanism.
   Do not configure real broker/payment credentials or enable live trading.
6. Exercise representative reads: health/version, dataset metadata and
   canonical records, and a stored backtest result. Verify that stored
   artifacts are served only after the application's own digest checks.
7. Record which snapshot was used, the oldest recoverable capture time, the
   measured time from restore start to verified application reads, any failed
   checks, and the required follow-up. Compare measured recovery time with the
   proposed 8-hour RTO and the recovery point with the proposed 24-hour RPO.

Passing an offline bundle verification is not a restore rehearsal. A rehearsal
is not complete until the disposable database and storage have been restored,
their references and hashes checked, and representative application reads
have succeeded.
