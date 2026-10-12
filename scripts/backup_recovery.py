"""Host-side, coordinated Restic backup tooling for a single Compose deployment.

This module is intentionally separate from application startup. It is invoked
explicitly on the deployment host and never reads or emits secret values.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import IO

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SERVICE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_REHEARSAL_PROJECT_NAME = re.compile(
    r"^iluvtrade-rehearsal-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_MANIFEST_NAME = "manifest.json"
_CHECKSUMS_NAME = "storage-checksums.jsonl"


class BackupError(RuntimeError):
    """A backup could not be safely captured or verified."""


@dataclass(frozen=True)
class BackupConfig:
    project_dir: Path
    compose_file: Path
    db_service: str
    storage_service: str
    storage_path: str
    staging_dir: Path
    restic_repository: str
    restic_password_file: Path | None
    backup_tag: str
    deployment_id: str
    deployment_tag: str
    stop_timeout: int
    app_commit: str
    compose_project_name: str | None = None
    rehearsal: bool = False
    docker_command: str = "docker"
    restic_command: str = "restic"

    @classmethod
    def from_environment(
        cls,
        environment: dict[str, str] | None = None,
        *,
        capture_only: bool = False,
    ) -> BackupConfig:
        env = os.environ if environment is None else environment
        project_dir = Path(env.get("ILUVTRADE_BACKUP_PROJECT_DIR", _REPO_ROOT)).expanduser()
        if not project_dir.is_absolute():
            project_dir = _REPO_ROOT / project_dir
        compose_file = Path(
            env.get("ILUVTRADE_BACKUP_COMPOSE_FILE", project_dir / "docker-compose.yml")
        ).expanduser()
        if not compose_file.is_absolute():
            compose_file = project_dir / compose_file
        staging_value = env.get("ILUVTRADE_BACKUP_STAGING_DIR", "").strip()
        repository = env.get("RESTIC_REPOSITORY", "").strip()
        password_value = env.get("RESTIC_PASSWORD_FILE", "").strip()
        backup_tag = env.get("ILUVTRADE_BACKUP_TAG", "").strip()
        deployment_id = env.get("ILUVTRADE_BACKUP_DEPLOYMENT_ID", "").strip()

        required = {
            "ILUVTRADE_BACKUP_STAGING_DIR": staging_value,
            "ILUVTRADE_BACKUP_DEPLOYMENT_ID": deployment_id,
            "ILUVTRADE_BACKUP_STAGING_ENCRYPTED": env.get(
                "ILUVTRADE_BACKUP_STAGING_ENCRYPTED", ""
            ).strip(),
            "ILUVTRADE_BACKUP_APP_COMMIT": env.get("ILUVTRADE_BACKUP_APP_COMMIT", "").strip(),
        }
        if not capture_only:
            required.update(
                {
                    "RESTIC_REPOSITORY": repository,
                    "RESTIC_PASSWORD_FILE": password_value,
                    "B2_ACCOUNT_ID": env.get("B2_ACCOUNT_ID", "").strip(),
                    "B2_ACCOUNT_KEY": env.get("B2_ACCOUNT_KEY", "").strip(),
                    "ILUVTRADE_BACKUP_TAG": backup_tag,
                }
            )
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise BackupError(f"Required backup configuration is missing: {', '.join(missing)}.")
        if required["ILUVTRADE_BACKUP_STAGING_ENCRYPTED"] != "1":
            raise BackupError(
                "ILUVTRADE_BACKUP_STAGING_ENCRYPTED must be 1 after independently verifying "
                "that the staging filesystem is encrypted at rest."
            )
        if not capture_only:
            repository_parts = repository.split(":", 2)
            if (
                not repository.startswith("b2:")
                or len(repository_parts) != 3
                or not repository_parts[1]
                or not repository_parts[2]
            ):
                raise BackupError("RESTIC_REPOSITORY must identify a Backblaze B2 repository.")

        db_service = env.get("ILUVTRADE_BACKUP_DB_SERVICE", "db")
        storage_service = env.get("ILUVTRADE_BACKUP_STORAGE_SERVICE", "api")
        for name, value in (
            ("ILUVTRADE_BACKUP_DB_SERVICE", db_service),
            ("ILUVTRADE_BACKUP_STORAGE_SERVICE", storage_service),
        ):
            if not _SERVICE_NAME.fullmatch(value):
                raise BackupError(f"{name} is not a valid Compose service name.")
        if db_service == storage_service:
            raise BackupError("The database and storage services must be distinct.")
        if backup_tag and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", backup_tag):
            raise BackupError(
                "ILUVTRADE_BACKUP_TAG must contain only letters, digits, '.', '_' or '-'."
            )
        try:
            canonical_deployment_id = str(uuid.UUID(deployment_id))
        except ValueError as exc:
            raise BackupError("ILUVTRADE_BACKUP_DEPLOYMENT_ID must be a canonical UUID.") from exc
        if deployment_id != canonical_deployment_id:
            raise BackupError("ILUVTRADE_BACKUP_DEPLOYMENT_ID must be a lowercase canonical UUID.")
        app_commit = env["ILUVTRADE_BACKUP_APP_COMMIT"].strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", app_commit):
            raise BackupError("ILUVTRADE_BACKUP_APP_COMMIT must be a full Git commit hash.")

        storage_path = env.get("ILUVTRADE_BACKUP_STORAGE_PATH", "/var/lib/iluvtrade/storage")
        posix_storage_path = PurePosixPath(storage_path)
        if not posix_storage_path.is_absolute() or ".." in posix_storage_path.parts:
            raise BackupError("ILUVTRADE_BACKUP_STORAGE_PATH must be an absolute safe path.")
        if posix_storage_path == PurePosixPath("/"):
            raise BackupError("The container root cannot be used as the storage root.")

        password_file: Path | None = None
        if password_value:
            password_file = Path(password_value).expanduser()
            if password_file.is_symlink():
                raise BackupError("RESTIC_PASSWORD_FILE must not be a symbolic link.")
            try:
                password_stat = password_file.stat()
            except OSError as exc:
                raise BackupError("RESTIC_PASSWORD_FILE is missing or inaccessible.") from exc
            if (
                not stat.S_ISREG(password_stat.st_mode)
                or stat.S_IMODE(password_stat.st_mode) & 0o077
                or not os.access(password_file, os.R_OK)
            ):
                raise BackupError(
                    "RESTIC_PASSWORD_FILE must be a regular file with mode 0600 or stricter."
                )

        project_dir = project_dir.resolve()
        compose_file = compose_file.resolve()
        if not project_dir.is_dir() or not compose_file.is_file():
            raise BackupError("The Compose project directory or file is missing.")
        staging_dir = Path(staging_value).expanduser()
        if not staging_dir.is_absolute():
            raise BackupError("ILUVTRADE_BACKUP_STAGING_DIR must be an absolute path.")
        if staging_dir.exists() and (not staging_dir.is_dir() or staging_dir.is_symlink()):
            raise BackupError("ILUVTRADE_BACKUP_STAGING_DIR must be a real directory.")
        if not staging_dir.exists() and not staging_dir.parent.is_dir():
            raise BackupError("The parent of ILUVTRADE_BACKUP_STAGING_DIR must exist.")
        if staging_dir.resolve().is_relative_to(project_dir.resolve()):
            raise BackupError(
                "The staging directory must be outside the Compose project directory."
            )
        parent = staging_dir if staging_dir.exists() else staging_dir.parent
        if not os.access(parent, os.W_OK | os.X_OK):
            raise BackupError("The recovery staging directory is not writable by this process.")

        try:
            stop_timeout = int(env.get("ILUVTRADE_BACKUP_STOP_TIMEOUT", "60"))
        except ValueError as exc:
            raise BackupError("ILUVTRADE_BACKUP_STOP_TIMEOUT must be an integer.") from exc
        if not 5 <= stop_timeout <= 600:
            raise BackupError("ILUVTRADE_BACKUP_STOP_TIMEOUT must be between 5 and 600 seconds.")

        return cls(
            project_dir=project_dir,
            compose_file=compose_file,
            db_service=db_service,
            storage_service=storage_service,
            storage_path=str(posix_storage_path),
            staging_dir=staging_dir.resolve(),
            restic_repository=repository,
            restic_password_file=password_file.resolve() if password_file is not None else None,
            backup_tag=backup_tag,
            deployment_id=deployment_id,
            deployment_tag=f"deployment-{deployment_id}",
            stop_timeout=stop_timeout,
            app_commit=app_commit,
            compose_project_name=None,
            rehearsal=False,
            docker_command=env.get("ILUVTRADE_BACKUP_DOCKER", "docker"),
            restic_command=env.get("ILUVTRADE_BACKUP_RESTIC", "restic"),
        )


def _checked_run(
    command: Sequence[str],
    *,
    stage: str,
    cwd: Path | None = None,
    stdout: int | IO[bytes] = subprocess.PIPE,
    environment: dict[str, str] | None = None,
) -> bytes:
    try:
        result = subprocess.run(
            list(command),
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise BackupError(f"{stage} could not be started.") from exc
    if result.returncode:
        raise BackupError(
            f"{stage} failed (exit code {result.returncode}); command output suppressed."
        )
    if stdout == subprocess.PIPE:
        if result.stdout is None:
            raise BackupError(f"{stage} produced no output.")
        return result.stdout
    return b""


def _decode_lines(payload: bytes, stage: str) -> list[str]:
    try:
        return [line.strip() for line in payload.decode("utf-8").splitlines() if line.strip()]
    except UnicodeDecodeError as exc:
        raise BackupError(f"{stage} returned invalid text.") from exc


def _compose(config: BackupConfig, *arguments: str) -> list[str]:
    command = [
        config.docker_command,
        "compose",
        "--project-directory",
        str(config.project_dir),
        "--file",
        str(config.compose_file),
    ]
    if config.compose_project_name is not None:
        command.extend(["--project-name", config.compose_project_name])
    return [*command, *arguments]


def _rehearsal_config(
    config: BackupConfig,
    environment: dict[str, str] | None = None,
) -> BackupConfig:
    env = os.environ if environment is None else environment
    project_name = env.get("ILUVTRADE_BACKUP_REHEARSAL_PROJECT_NAME", "").strip()
    if not _REHEARSAL_PROJECT_NAME.fullmatch(project_name):
        raise BackupError(
            "ILUVTRADE_BACKUP_REHEARSAL_PROJECT_NAME must be "
            "'iluvtrade-rehearsal-' followed by a unique lowercase UUID."
        )
    if env.get("ILUVTRADE_BACKUP_REHEARSAL_ISOLATION_CONFIRMED") != "1":
        raise BackupError(
            "Set ILUVTRADE_BACKUP_REHEARSAL_ISOLATION_CONFIRMED=1 only after confirming "
            "the disposable Docker context, project directory, database, and storage."
        )
    if not env.get("ILUVTRADE_BACKUP_PROJECT_DIR", "").strip():
        raise BackupError(
            "ILUVTRADE_BACKUP_PROJECT_DIR must explicitly identify the disposable Compose "
            "project directory."
        )
    if not env.get("ILUVTRADE_BACKUP_COMPOSE_FILE", "").strip():
        raise BackupError(
            "ILUVTRADE_BACKUP_COMPOSE_FILE must explicitly identify the rehearsal Compose file."
        )
    if not Path(env["ILUVTRADE_BACKUP_PROJECT_DIR"]).expanduser().is_absolute():
        raise BackupError("The rehearsal Compose project directory must be an absolute path.")
    if not Path(env["ILUVTRADE_BACKUP_COMPOSE_FILE"]).expanduser().is_absolute():
        raise BackupError("The rehearsal Compose file path must be absolute.")
    if not config.compose_file.is_relative_to(config.project_dir):
        raise BackupError("The rehearsal Compose file must be inside its project directory.")
    if (
        config.project_dir == _REPO_ROOT
        or config.project_dir.is_relative_to(_REPO_ROOT)
        or _REPO_ROOT.is_relative_to(config.project_dir)
    ):
        raise BackupError(
            "The rehearsal Compose project directory must be separate from the application "
            "repository."
        )
    inferred_name = re.sub(r"[^a-z0-9_-]", "", config.project_dir.name.lower())
    if inferred_name == project_name:
        raise BackupError(
            "The rehearsal project name must differ from the Compose directory-derived identity."
        )
    inherited_project = env.get("COMPOSE_PROJECT_NAME", "").strip()
    if inherited_project and inherited_project != project_name:
        raise BackupError(
            "COMPOSE_PROJECT_NAME conflicts with the explicit rehearsal project identity."
        )
    return replace(config, compose_project_name=project_name, rehearsal=True)


def _compose_text(config: BackupConfig, *arguments: str, stage: str) -> list[str]:
    return _decode_lines(
        _checked_run(
            _compose(config, *arguments),
            stage=stage,
            cwd=config.project_dir,
        ),
        stage,
    )


def _restic_environment(config: BackupConfig) -> dict[str, str]:
    if config.restic_password_file is None:
        raise BackupError("RESTIC_PASSWORD_FILE is required for Restic operations.")
    environment = os.environ.copy()
    environment.pop("RESTIC_PASSWORD", None)
    environment.pop("RESTIC_PASSWORD_COMMAND", None)
    environment["RESTIC_REPOSITORY"] = config.restic_repository
    environment["RESTIC_PASSWORD_FILE"] = str(config.restic_password_file)
    return environment


def _restic(
    config: BackupConfig,
    *arguments: str,
    stage: str,
    stdout: int | IO[bytes] = subprocess.PIPE,
) -> bytes:
    return _checked_run(
        [config.restic_command, *arguments],
        stage=stage,
        cwd=config.project_dir,
        stdout=stdout,
        environment=_restic_environment(config),
    )


@contextmanager
def _backup_lock(staging_dir: Path) -> Iterator[None]:
    staging_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(staging_dir, 0o700)
    lock_path = staging_dir / ".backup.lock"
    try:
        descriptor = os.open(
            lock_path,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        raise BackupError("The exclusive backup lock could not be opened safely.") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise BackupError("The exclusive backup lock is not a regular file.")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackupError("Another backup operation holds the recovery lock.") from exc
        yield
    finally:
        os.close(descriptor)


def _services(config: BackupConfig) -> tuple[set[str], set[str], set[str]]:
    configured = set(
        _compose_text(config, "config", "--services", stage="Compose service discovery")
    )
    if config.db_service not in configured:
        raise BackupError("Configured database service is not present in the Compose project.")
    if config.storage_service not in configured:
        raise BackupError("Configured storage service is not present in the Compose project.")

    all_project_services = set(
        _compose_text(
            config,
            "ps",
            "--all",
            "--orphans",
            "--services",
            stage="Compose container discovery",
        )
    )
    unexpected = all_project_services - configured
    if unexpected:
        raise BackupError(
            "Compose orphan containers were found; add or remove them from the project before "
            "backup."
        )
    running = set(
        _compose_text(
            config,
            "ps",
            "--status",
            "running",
            "--services",
            stage="Compose running-service discovery",
        )
    )
    unexpected_running = running - configured
    if unexpected_running:
        raise BackupError("A running Compose service is not declared in the configured project.")
    if config.db_service not in running:
        raise BackupError("The PostgreSQL service must already be running; it will not be stopped.")
    return configured, running, configured - {config.db_service}


def _service_container_ids(config: BackupConfig, service: str) -> list[str]:
    return _compose_text(
        config,
        "ps",
        "--all",
        "--quiet",
        service,
        stage="Compose container lookup",
    )


def _validate_rehearsal_compose_volumes(config: BackupConfig) -> None:
    if not config.rehearsal or config.compose_project_name is None:
        raise BackupError("The rehearsal Compose project identity was not validated.")
    output = _checked_run(
        _compose(config, "config", "--format", "json"),
        stage="Rehearsal Compose configuration validation",
        cwd=config.project_dir,
    )
    try:
        effective_config = json.loads(output)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError(
            "The effective rehearsal Compose configuration could not be parsed."
        ) from exc
    if not isinstance(effective_config, dict):
        raise BackupError("The effective rehearsal Compose configuration is invalid.")
    if not isinstance(effective_config.get("services"), dict):
        raise BackupError("The effective rehearsal Compose service declarations are invalid.")
    volumes = effective_config.get("volumes", {})
    if not isinstance(volumes, dict):
        raise BackupError("The effective rehearsal Compose volume declarations are invalid.")
    for volume in volumes.values():
        if not isinstance(volume, dict):
            raise BackupError("The effective rehearsal Compose volume declaration is invalid.")
        if "external" in volume and volume["external"] is not False:
            raise BackupError("External Compose volumes are refused for a capture-only rehearsal.")


def _validate_rehearsal_containers(config: BackupConfig, services: set[str]) -> None:
    project_name = config.compose_project_name
    if not config.rehearsal or project_name is None:
        raise BackupError("The rehearsal Compose project identity was not validated.")
    template = (
        '{{index .Config.Labels "com.docker.compose.project"}}{{"\\n"}}'
        '{{range .Mounts}}{{.Type}}|{{.Name}}|{{.Source}}{{"\\n"}}{{end}}'
    )
    for service in sorted(services):
        for container_id in _service_container_ids(config, service):
            lines = _decode_lines(
                _checked_run(
                    [
                        config.docker_command,
                        "inspect",
                        "--format",
                        template,
                        container_id,
                    ],
                    stage="Rehearsal Compose project verification",
                ),
                "Rehearsal Compose project verification",
            )
            if not lines or lines[0] != project_name:
                raise BackupError(
                    "A rehearsal container does not carry the explicit disposable project label."
                )
            for mount in lines[1:]:
                fields = mount.split("|", 2)
                if len(fields) != 3:
                    raise BackupError("A rehearsal container has an unrecognized mount.")
                mount_type, volume_name, _source = fields
                if mount_type == "bind":
                    raise BackupError(
                        "Rehearsal bind mounts are refused; use project-scoped named volumes."
                    )
                if mount_type == "tmpfs":
                    continue
                if mount_type != "volume" or not volume_name.startswith(f"{project_name}_"):
                    raise BackupError(
                        "A rehearsal container uses a non-project-scoped persistent volume."
                    )
                volume_project = _decode_lines(
                    _checked_run(
                        [
                            config.docker_command,
                            "volume",
                            "inspect",
                            "--format",
                            '{{index .Labels "com.docker.compose.project"}}',
                            volume_name,
                        ],
                        stage="Rehearsal volume ownership verification",
                    ),
                    "Rehearsal volume ownership verification",
                )
                if volume_project != [project_name]:
                    raise BackupError(
                        "A rehearsal volume is not labeled as owned by the disposable project."
                    )


def _container_mount_sources(config: BackupConfig, container_id: str) -> list[Path]:
    template = '{{range .Mounts}}{{.Source}}{{"\\n"}}{{end}}'
    output = _checked_run(
        [config.docker_command, "inspect", "--format", template, container_id],
        stage="Persistent mount inspection",
    )
    return [
        Path(source).resolve() for source in _decode_lines(output, "Persistent mount inspection")
    ]


def _ensure_staging_outside_mounts(config: BackupConfig, container_ids: list[str]) -> None:
    staging_dir = config.staging_dir.resolve()
    for container_id in container_ids:
        for source in _container_mount_sources(config, container_id):
            if staging_dir.is_relative_to(source) or source.is_relative_to(staging_dir):
                raise BackupError(
                    "The recovery staging directory overlaps an application data mount."
                )


def _check_database_has_no_other_clients(config: BackupConfig) -> None:
    sql = (
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE datname = current_database() "
        "AND backend_type = 'client backend' "
        "AND pid <> pg_backend_pid()"
    )
    result = _checked_run(
        _compose(
            config,
            "exec",
            "--no-TTY",
            config.db_service,
            "sh",
            "-ec",
            'psql -X -A -t -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" '
            f"-c {shlex.quote(sql)}",
        ),
        stage="PostgreSQL writer check",
        cwd=config.project_dir,
    )
    clients = _decode_lines(result, "PostgreSQL writer check")
    if clients != ["0"]:
        raise BackupError(
            "PostgreSQL has other client connections; external writers or operators must be "
            "quiesced before backup."
        )


def _database_command(config: BackupConfig, program: str, *arguments: str) -> list[str]:
    shell_command = (
        'test -n "$POSTGRES_DB" && test -n "$POSTGRES_USER" && '
        f'{program} -X -A -t -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" '
        f'-d "$POSTGRES_DB" {" ".join(shlex.quote(argument) for argument in arguments)}'
    )
    return _compose(
        config,
        "exec",
        "--no-TTY",
        config.db_service,
        "sh",
        "-ec",
        shell_command,
    )


def _alembic_revision(config: BackupConfig) -> str:
    result = _checked_run(
        _database_command(
            config,
            "psql",
            "-c",
            "SELECT version_num FROM alembic_version",
        ),
        stage="Alembic revision query",
        cwd=config.project_dir,
    )
    revisions = _decode_lines(result, "Alembic revision query")
    if len(revisions) != 1 or not re.fullmatch(r"[A-Za-z0-9_.-]+", revisions[0]):
        raise BackupError("The database did not report exactly one valid Alembic revision.")
    return revisions[0]


def _application_identity(config: BackupConfig) -> tuple[str, str]:
    pyproject = config.project_dir / "backend" / "pyproject.toml"
    try:
        with pyproject.open("rb") as stream:
            version = tomllib.load(stream)["project"]["version"]
    except (OSError, KeyError, tomllib.TOMLDecodeError, TypeError) as exc:
        raise BackupError(
            "The application version could not be read from backend/pyproject.toml."
        ) from exc
    if not isinstance(version, str) or not version:
        raise BackupError("The application version in backend/pyproject.toml is invalid.")
    return version, config.app_commit


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _storage_checksums(storage_root: Path, output: Path) -> tuple[str, int]:
    count = 0
    output_digest = hashlib.sha256()

    def fail_walk(error: OSError) -> None:
        raise BackupError("The storage root could not be completely enumerated.") from error

    with output.open("wb") as manifest:
        os.chmod(output, 0o600)
        for current, directories, files in os.walk(
            storage_root,
            followlinks=False,
            onerror=fail_walk,
        ):
            directories.sort()
            files.sort()
            current_path = Path(current)
            for name in directories:
                if (current_path / name).is_symlink():
                    raise BackupError("The storage root contains a symbolic link; backup refused.")
            for name in files:
                path = current_path / name
                if path.is_symlink():
                    raise BackupError("The storage root contains a symbolic link; backup refused.")
                if not path.is_file():
                    raise BackupError(
                        "The storage root contains a non-regular file; backup refused."
                    )
                relative = path.relative_to(storage_root).as_posix()
                record = {
                    "path": relative,
                    "size_bytes": path.stat().st_size,
                    "sha256": _sha256_file(path),
                }
                encoded = (
                    json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
                ).encode()
                manifest.write(encoded)
                output_digest.update(encoded)
                count += 1
        manifest.flush()
        os.fsync(manifest.fileno())
    return output_digest.hexdigest(), count


def _archive_storage(storage_root: Path, archive_path: Path) -> None:
    with tarfile.open(archive_path, mode="w") as archive:
        archive.add(storage_root, arcname="storage", recursive=True, filter=_safe_tar_info)
    os.chmod(archive_path, 0o600)
    with archive_path.open("rb") as stream:
        os.fsync(stream.fileno())


def _verify_storage_archive(archive_path: Path, checksum_path: Path) -> None:
    expected: dict[str, tuple[int, str]] = {}
    try:
        with checksum_path.open(encoding="utf-8") as stream:
            for line in stream:
                record = json.loads(line)
                relative = PurePosixPath(record["path"])
                if relative.is_absolute() or ".." in relative.parts:
                    raise BackupError("The storage checksum manifest contains an unsafe path.")
                name = relative.as_posix()
                if name in expected:
                    raise BackupError("The storage checksum manifest contains duplicate paths.")
                if not isinstance(record["size_bytes"], int) or record["size_bytes"] < 0:
                    raise BackupError(
                        "The storage checksum manifest contains an invalid file size."
                    )
                if not re.fullmatch(r"[0-9a-f]{64}", record["sha256"]):
                    raise BackupError(
                        "The storage checksum manifest contains an invalid file hash."
                    )
                expected[name] = (record["size_bytes"], record["sha256"])
        with tarfile.open(archive_path, mode="r:") as archive:
            members = {}
            for member in archive.getmembers():
                if member.name == "storage":
                    continue
                path = PurePosixPath(member.name)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or not path.parts
                    or path.parts[0] != "storage"
                    or not (member.isfile() or member.isdir())
                ):
                    raise BackupError("The storage archive contains an unsafe filesystem entry.")
                if member.isfile():
                    members[path.relative_to(PurePosixPath("storage")).as_posix()] = member
            if set(members) != set(expected):
                raise BackupError(
                    "The storage archive contents do not match the checksum manifest."
                )
            for name, member in members.items():
                payload = archive.extractfile(member)
                if payload is None:
                    raise BackupError("A storage archive file could not be read.")
                digest = hashlib.sha256()
                size = 0
                while chunk := payload.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
                expected_size, expected_digest = expected[name]
                if size != expected_size or digest.hexdigest() != expected_digest:
                    raise BackupError(f"Storage archive content failed verification: {name}.")
    except (OSError, tarfile.TarError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise BackupError("The storage archive or checksum manifest is invalid.") from exc


def _safe_tar_info(info: tarfile.TarInfo) -> tarfile.TarInfo:
    if not (info.isfile() or info.isdir()):
        raise BackupError("The storage root contains an unsupported filesystem object.")
    return info


def _capture_bundle(config: BackupConfig, *, preserve_failed: bool = False) -> Path:
    configured_services, initially_running, writer_services = _services(config)
    if config.storage_service not in writer_services:
        raise BackupError("The storage service must be one of the quiesced application services.")

    storage_containers = _service_container_ids(config, config.storage_service)
    if not storage_containers:
        raise BackupError(
            "No storage-service container exists to read the persistent storage root."
        )
    database_containers = _service_container_ids(config, config.db_service)
    if len(database_containers) != 1:
        raise BackupError("Exactly one configured PostgreSQL service container is required.")
    if config.rehearsal:
        _validate_rehearsal_containers(config, configured_services)
    _ensure_staging_outside_mounts(config, database_containers)
    _ensure_staging_outside_mounts(config, storage_containers)
    mount_sources = []
    mount_template = (
        "{{range .Mounts}}{{if eq .Destination "
        f'"{config.storage_path}"'
        "}}{{.Type}}|{{.Name}}|{{.Source}}{{end}}{{end}}"
    )
    for container_id in storage_containers:
        source = _decode_lines(
            _checked_run(
                [config.docker_command, "inspect", "--format", mount_template, container_id],
                stage="Storage mount verification",
            ),
            "Storage mount verification",
        )
        if (
            len(source) != 1
            or source[0].split("|", 1)[0] not in {"volume", "bind"}
            or not source[0].split("|", 1)[-1]
        ):
            raise BackupError("The configured storage path is not on a persistent verified mount.")
        mount_sources.append(source[0])
    if len(set(mount_sources)) != 1:
        raise BackupError("Scaled storage service containers have different storage mounts.")

    timestamp = datetime.now(UTC)
    bundle_name = f"recovery-{timestamp.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    temporary = Path(tempfile.mkdtemp(prefix=".capture-", dir=config.staging_dir))
    os.chmod(temporary, 0o700)
    completed_bundle = config.staging_dir / bundle_name
    stopped = False
    captured_bundle: Path | None = None
    capture_error: BaseException | None = None
    failed_bundle: Path | None = None
    evidence_error: OSError | None = None
    try:
        running_writers = sorted(initially_running & writer_services)
        if running_writers:
            stopped = True
            _checked_run(
                _compose(
                    config,
                    "stop",
                    "--timeout",
                    str(config.stop_timeout),
                    *running_writers,
                ),
                stage="Graceful application maintenance stop",
                cwd=config.project_dir,
            )
        remaining = set(
            _compose_text(
                config,
                "ps",
                "--status",
                "running",
                "--services",
                stage="Application writer quiescence check",
            )
        )
        if remaining & writer_services:
            raise BackupError("At least one Compose application writer is still running.")
        if config.db_service not in remaining:
            raise BackupError("The PostgreSQL service stopped unexpectedly; backup aborted.")

        _check_database_has_no_other_clients(config)
        dump_path = temporary / "database.dump"
        with dump_path.open("wb") as dump:
            os.chmod(dump_path, 0o600)
            _checked_run(
                _compose(
                    config,
                    "exec",
                    "--no-TTY",
                    config.db_service,
                    "sh",
                    "-ec",
                    "pg_dump --format=custom --no-owner --no-acl "
                    '-U "$POSTGRES_USER" -d "$POSTGRES_DB"',
                ),
                stage="PostgreSQL consistent dump",
                cwd=config.project_dir,
                stdout=dump,
            )
            dump.flush()
            os.fsync(dump.fileno())
        if dump_path.stat().st_size == 0:
            raise BackupError("PostgreSQL produced an empty dump.")

        revision = _alembic_revision(config)

        copied_storage = temporary / "storage"
        copied_storage.mkdir(mode=0o700)
        _checked_run(
            [
                config.docker_command,
                "cp",
                f"{storage_containers[0]}:{config.storage_path}/.",
                str(copied_storage),
            ],
            stage="Persistent storage capture",
        )
        checksum_path = temporary / _CHECKSUMS_NAME
        storage_manifest_hash, storage_file_count = _storage_checksums(
            copied_storage, checksum_path
        )
        archive_path = temporary / "storage.tar"
        _archive_storage(copied_storage, archive_path)
        shutil.rmtree(copied_storage)

        _check_database_has_no_other_clients(config)
        version, commit = _application_identity(config)
        compose_hash = _sha256_file(config.compose_file)
        manifest = {
            "format_version": 1,
            "backup_id": bundle_name,
            "capture_started_at": timestamp.isoformat(),
            "captured_at": datetime.now(UTC).isoformat(),
            "application": {"version": version, "commit": commit},
            "alembic_revision": revision,
            "deployment": {
                "id": config.deployment_id,
                "compose_file_sha256": compose_hash,
            },
            "checksums": {
                "database.dump": _sha256_file(dump_path),
                "storage.tar": _sha256_file(archive_path),
                _CHECKSUMS_NAME: storage_manifest_hash,
            },
            "storage_file_count": storage_file_count,
        }
        manifest_path = temporary / _MANIFEST_NAME
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(manifest_path, 0o600)
        with manifest_path.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, completed_bundle)
        staging_descriptor = os.open(
            config.staging_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(staging_descriptor)
        finally:
            os.close(staging_descriptor)
        captured_bundle = completed_bundle
    except BaseException as exc:
        capture_error = exc
        if temporary.exists():
            if preserve_failed:
                failed_bundle = config.staging_dir / (
                    f"failed-capture-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-"
                    f"{uuid.uuid4().hex[:8]}"
                )
                try:
                    os.replace(temporary, failed_bundle)
                except OSError as move_error:
                    evidence_error = move_error
                    failed_bundle = temporary
            else:
                shutil.rmtree(temporary, ignore_errors=True)
        elif preserve_failed and completed_bundle.exists():
            failed_bundle = config.staging_dir / (
                f"failed-capture-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-"
                f"{uuid.uuid4().hex[:8]}"
            )
            try:
                os.replace(completed_bundle, failed_bundle)
            except OSError as move_error:
                evidence_error = move_error
                failed_bundle = completed_bundle

    resume_error: BackupError | None = None
    if stopped and initially_running & writer_services:
        try:
            _checked_run(
                _compose(config, "start", *sorted(initially_running & writer_services)),
                stage="Application writer resumption",
                cwd=config.project_dir,
            )
            resumed = set(
                _compose_text(
                    config,
                    "ps",
                    "--status",
                    "running",
                    "--services",
                    stage="Application writer resumption verification",
                )
            )
            if not (initially_running & writer_services).issubset(resumed):
                raise BackupError(
                    "One or more previously running application services did not resume."
                )
        except BackupError as exc:
            resume_error = exc

    if capture_error is not None:
        if not isinstance(capture_error, Exception):
            raise capture_error
        message = f"Capture failed: {capture_error}"
        if failed_bundle is not None:
            message += f" Partial capture evidence retained at {failed_bundle.resolve()}."
        if evidence_error is not None:
            message += f" Partial evidence could not be moved: {evidence_error}."
        if resume_error is not None:
            message += f" Writer resumption also failed: {resume_error}"
        raise BackupError(message) from capture_error
    if resume_error is not None:
        if captured_bundle is not None:
            raise BackupError(
                f"{resume_error} Completed local recovery bundle retained at "
                f"{captured_bundle.resolve()}."
            ) from resume_error
        raise resume_error
    if captured_bundle is None:
        raise BackupError("The capture finished without producing a recovery bundle.")
    return captured_bundle


def _verify_bundle(bundle: Path) -> dict[str, object]:
    if bundle.is_symlink() or not bundle.is_dir():
        raise BackupError("The recovery bundle must be a real directory.")
    try:
        manifest_path = bundle / _MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        checksums = manifest["checksums"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise BackupError("The recovery bundle manifest is missing or invalid.") from exc
    if not isinstance(checksums, dict):
        raise BackupError("The recovery bundle checksum map is invalid.")
    if (
        manifest.get("format_version") != 1
        or not isinstance(manifest.get("alembic_revision"), str)
        or not isinstance(manifest.get("storage_file_count"), int)
        or manifest["storage_file_count"] < 0
    ):
        raise BackupError("The recovery bundle manifest has an unsupported or invalid format.")
    for name in ("database.dump", "storage.tar", _CHECKSUMS_NAME):
        path = bundle / name
        if not path.is_file() or path.is_symlink() or checksums.get(name) != _sha256_file(path):
            raise BackupError(f"The recovery bundle failed checksum verification for {name}.")
    try:
        file_count = 0
        with (bundle / _CHECKSUMS_NAME).open(encoding="utf-8") as stream:
            for line in stream:
                record = json.loads(line)
                relative = PurePosixPath(record["path"])
                if relative.is_absolute() or ".." in relative.parts:
                    raise BackupError("The storage checksum manifest contains an unsafe path.")
                if not isinstance(record["size_bytes"], int) or not re.fullmatch(
                    r"[0-9a-f]{64}", record["sha256"]
                ):
                    raise BackupError("The storage checksum manifest contains an invalid record.")
                file_count += 1
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise BackupError("The storage checksum manifest is invalid.") from exc
    if file_count != manifest.get("storage_file_count"):
        raise BackupError("The storage checksum manifest has an unexpected file count.")
    _verify_storage_archive(bundle / "storage.tar", bundle / _CHECKSUMS_NAME)
    return manifest


def _verify_storage_references(checksum_path: Path, references_path: Path) -> tuple[int, int]:
    checksums: dict[str, str] = {}
    try:
        with checksum_path.open(encoding="utf-8") as stream:
            for line in stream:
                record = json.loads(line)
                relative = PurePosixPath(record["path"])
                digest = record["sha256"]
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or not re.fullmatch(r"[0-9a-f]{64}", digest)
                ):
                    raise BackupError("The storage checksum manifest contains an invalid record.")
                if relative.as_posix() in checksums:
                    raise BackupError("The storage checksum manifest contains duplicate paths.")
                checksums[relative.as_posix()] = digest

        reference_count = 0
        unhashed_count = 0
        with references_path.open(encoding="utf-8") as stream:
            for line in stream:
                row = line.rstrip("\n").split("\t")
                if len(row) != 2:
                    raise BackupError(
                        "Database storage references must be tab-separated path/hash rows."
                    )
                relative = PurePosixPath(row[0])
                if relative.is_absolute() or ".." in relative.parts:
                    raise BackupError("A database storage reference contains an unsafe path.")
                stored_digest = row[1].strip()
                if relative.as_posix() not in checksums:
                    raise BackupError(
                        "A database storage reference is absent from the recovery bundle."
                    )
                if stored_digest:
                    if not re.fullmatch(r"[0-9a-f]{64}", stored_digest):
                        raise BackupError(
                            "A database storage reference has an invalid recorded hash."
                        )
                    if checksums[relative.as_posix()] != stored_digest:
                        raise BackupError(
                            "A database storage reference hash does not match the bundle."
                        )
                else:
                    unhashed_count += 1
                reference_count += 1
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise BackupError("The storage reference verification input is invalid.") from exc
    return reference_count, unhashed_count


def _snapshot_id(backup_output: bytes) -> str:
    try:
        messages = [json.loads(line) for line in backup_output.decode("utf-8").splitlines() if line]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError("Restic returned invalid backup verification data.") from exc
    for message in reversed(messages):
        if message.get("message_type") == "summary":
            snapshot_id = message.get("snapshot_id")
            if isinstance(snapshot_id, str) and re.fullmatch(r"[0-9a-f]{8,64}", snapshot_id):
                return snapshot_id
    raise BackupError("Restic did not report a verified snapshot identifier.")


def _snapshot_exists(config: BackupConfig, snapshot_id: str) -> bool:
    output = _restic(
        config,
        "snapshots",
        "--json",
        "--tag",
        config.deployment_tag,
        stage="Restic snapshot verification",
    )
    try:
        snapshots = json.loads(output)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError("Restic returned invalid snapshot-list data.") from exc
    if not isinstance(snapshots, list):
        raise BackupError("Restic returned an invalid snapshot list for this deployment.")
    for snapshot in snapshots:
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("tags"), list):
            raise BackupError("Restic returned a snapshot without valid ownership tags.")
        tags = snapshot["tags"]
        if config.deployment_tag not in tags or config.backup_tag not in tags:
            raise BackupError(
                "Restic returned a snapshot with unexpected ownership tags; retention refused."
            )
    return any(snapshot.get("id") == snapshot_id for snapshot in snapshots)


def _upload_and_retain(config: BackupConfig, bundle: Path) -> str:
    _verify_bundle(bundle)
    output = _restic(
        config,
        "backup",
        "--json",
        "--tag",
        config.backup_tag,
        "--tag",
        config.deployment_tag,
        str(bundle),
        stage="Restic encrypted offsite backup",
    )
    snapshot_id = _snapshot_id(output)
    if not _snapshot_exists(config, snapshot_id):
        raise BackupError("The new Restic snapshot was not found; retention was not applied.")

    _restic(
        config,
        "forget",
        "--keep-daily",
        "7",
        "--keep-weekly",
        "4",
        "--keep-monthly",
        "6",
        "--keep-last",
        "2",
        "--group-by",
        "tags",
        "--tag",
        config.deployment_tag,
        "--prune",
        stage="Restic retention",
    )
    if not _snapshot_exists(config, snapshot_id):
        raise BackupError("The new Restic snapshot is missing after retention; bundle retained.")
    return snapshot_id


def _perform_backup(config: BackupConfig) -> str:
    with _backup_lock(config.staging_dir):
        _restic(config, "snapshots", "--json", stage="Restic repository validation")
        bundle = _capture_bundle(config)
        try:
            snapshot_id = _upload_and_retain(config, bundle)
        except BackupError as exc:
            raise BackupError(
                f"{exc} Local recovery bundle retained at {bundle}; retry with "
                f"--upload-bundle {bundle}."
            ) from exc
        shutil.rmtree(bundle)
        return snapshot_id


def _capture_only(config: BackupConfig) -> Path:
    _validate_rehearsal_compose_volumes(config)
    with _backup_lock(config.staging_dir):
        bundle = _capture_bundle(config, preserve_failed=True)
        try:
            _verify_bundle(bundle)
        except BackupError as exc:
            raise BackupError(
                f"The local recovery bundle failed verification and was retained at "
                f"{bundle.resolve()}."
            ) from exc
        return bundle.resolve()


def _upload_pending(config: BackupConfig, bundle: Path) -> str:
    requested_bundle = bundle.expanduser()
    if requested_bundle.is_symlink():
        raise BackupError("Pending recovery bundles must not be symbolic links.")
    resolved_bundle = requested_bundle.resolve()
    try:
        resolved_bundle.relative_to(config.staging_dir)
    except ValueError as exc:
        raise BackupError(
            "Pending recovery bundles must be inside the configured staging directory."
        ) from exc
    if resolved_bundle == config.staging_dir or resolved_bundle.parent != config.staging_dir:
        raise BackupError("The requested path is not a direct child recovery bundle.")
    with _backup_lock(config.staging_dir):
        _restic(config, "snapshots", "--json", stage="Restic repository validation")
        try:
            snapshot_id = _upload_and_retain(config, resolved_bundle)
        except BackupError as exc:
            raise BackupError(
                f"{exc} Local recovery bundle retained at {resolved_bundle}."
            ) from exc
        shutil.rmtree(resolved_bundle)
        return snapshot_id


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture a coordinated Compose/PostgreSQL/storage recovery point to Restic/B2."
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--execute",
        action="store_true",
        help="Capture and upload a recovery bundle. Without this flag, no commands are run.",
    )
    actions.add_argument(
        "--capture-only",
        action="store_true",
        help="Capture and verify a local bundle for an explicitly isolated rehearsal; no Restic.",
    )
    actions.add_argument(
        "--upload-bundle",
        type=Path,
        help="Upload and verify a previously captured local bundle without stopping services.",
    )
    actions.add_argument(
        "--verify-bundle",
        type=Path,
        help="Verify a local recovery bundle's checksums without contacting Docker or Restic.",
    )
    actions.add_argument(
        "--verify-references",
        nargs=2,
        type=Path,
        metavar=("STORAGE_CHECKSUMS_JSONL", "DATABASE_REFERENCES_TSV"),
        help="Verify restored DB file references and recorded hashes against the storage manifest.",
    )
    args = parser.parse_args(argv)
    try:
        if args.verify_bundle is not None:
            manifest = _verify_bundle(args.verify_bundle.expanduser())
            print(
                "Local recovery bundle verified "
                f"(Alembic revision {manifest['alembic_revision']}, "
                f"{manifest['storage_file_count']} storage files)."
            )
            return 0
        if args.verify_references is not None:
            checked, unhashed = _verify_storage_references(
                args.verify_references[0].expanduser(),
                args.verify_references[1].expanduser(),
            )
            print(
                f"Verified {checked} database storage references; "
                f"{unhashed} have no application-recorded content hash."
            )
            return 0
        config = BackupConfig.from_environment(capture_only=args.capture_only)
        if args.capture_only:
            config = _rehearsal_config(config)
            bundle = _capture_only(config)
            print(f"Local recovery bundle captured and verified: {bundle}")
            return 0
        if args.upload_bundle is not None:
            snapshot_id = _upload_pending(config, args.upload_bundle)
            print(f"Backup verified; retention applied. Snapshot: {snapshot_id[:12]}")
        elif args.execute:
            snapshot_id = _perform_backup(config)
            print(f"Backup verified; retention applied. Snapshot: {snapshot_id[:12]}")
        else:
            print(
                "Dry run only: configuration is valid. No Docker, database, or Restic commands "
                "were run. Use --execute to capture and upload a backup."
            )
    except BackupError as exc:
        print(f"Backup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
