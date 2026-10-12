from __future__ import annotations

import hashlib
import importlib.util
import json
import stat
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest

_BACKUP_SPEC = importlib.util.spec_from_file_location(
    "iluvtrade_backup_recovery",
    Path(__file__).resolve().parents[3] / "scripts" / "backup_recovery.py",
)
assert _BACKUP_SPEC is not None and _BACKUP_SPEC.loader is not None
backup = importlib.util.module_from_spec(_BACKUP_SPEC)
sys.modules[_BACKUP_SPEC.name] = backup
_BACKUP_SPEC.loader.exec_module(backup)


@pytest.fixture
def backup_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    root = tmp_path / "project"
    (root / "backend").mkdir(parents=True)
    (root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (root / "backend" / "pyproject.toml").write_text(
        '[project]\nname = "iluvtrade"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    password_file = tmp_path / "restic-password"
    password_file.write_text("test-only-password\n", encoding="utf-8")
    password_file.chmod(0o600)
    values = {
        "ILUVTRADE_BACKUP_PROJECT_DIR": str(root),
        "ILUVTRADE_BACKUP_COMPOSE_FILE": str(root / "docker-compose.yml"),
        "ILUVTRADE_BACKUP_STAGING_DIR": str(staging),
        "RESTIC_REPOSITORY": "b2:test-bucket:test-prefix",
        "RESTIC_PASSWORD_FILE": str(password_file),
        "B2_ACCOUNT_ID": "test-account",
        "B2_ACCOUNT_KEY": "test-account-key",
        "ILUVTRADE_BACKUP_TAG": "test-iluvtrade-deployment",
        "ILUVTRADE_BACKUP_DEPLOYMENT_ID": "9b2f8c6e-6c5a-4f7d-8a1b-3c4d5e6f7081",
        "ILUVTRADE_BACKUP_STAGING_ENCRYPTED": "1",
        "ILUVTRADE_BACKUP_APP_COMMIT": "a" * 40,
        "ILUVTRADE_BACKUP_REHEARSAL_PROJECT_NAME": (
            "iluvtrade-rehearsal-1b2f8c6e-6c5a-4f7d-8a1b-3c4d5e6f7082"
        ),
        "ILUVTRADE_BACKUP_REHEARSAL_ISOLATION_CONFIRMED": "1",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


def _completed(stdout: bytes = b"", returncode: int = 0) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=b"")


def test_default_invocation_is_dry_run_and_runs_no_commands(
    backup_environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        backup.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("dry run must not invoke subprocesses"),
    )

    assert backup.main([]) == 0
    assert "Dry run only" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("missing", "expected"),
    [
        ("RESTIC_REPOSITORY", "RESTIC_REPOSITORY"),
        ("RESTIC_PASSWORD_FILE", "RESTIC_PASSWORD_FILE"),
        ("B2_ACCOUNT_ID", "B2_ACCOUNT_ID"),
        ("B2_ACCOUNT_KEY", "B2_ACCOUNT_KEY"),
        ("ILUVTRADE_BACKUP_STAGING_DIR", "ILUVTRADE_BACKUP_STAGING_DIR"),
        ("ILUVTRADE_BACKUP_TAG", "ILUVTRADE_BACKUP_TAG"),
        ("ILUVTRADE_BACKUP_DEPLOYMENT_ID", "ILUVTRADE_BACKUP_DEPLOYMENT_ID"),
        ("ILUVTRADE_BACKUP_STAGING_ENCRYPTED", "ILUVTRADE_BACKUP_STAGING_ENCRYPTED"),
    ],
)
def test_missing_configuration_fails_closed(
    backup_environment: dict[str, str], missing: str, expected: str
) -> None:
    environment = backup_environment.copy()
    environment.pop(missing)

    with pytest.raises(backup.BackupError, match=expected):
        backup.BackupConfig.from_environment(environment)


def test_password_file_permissions_must_be_private(
    backup_environment: dict[str, str], tmp_path: Path
) -> None:
    password = Path(backup_environment["RESTIC_PASSWORD_FILE"])
    password.chmod(0o644)

    with pytest.raises(backup.BackupError, match="mode 0600"):
        backup.BackupConfig.from_environment()


def test_staging_encryption_requires_explicit_operator_confirmation(
    backup_environment: dict[str, str],
) -> None:
    environment = backup_environment.copy()
    environment["ILUVTRADE_BACKUP_STAGING_ENCRYPTED"] = "true"

    with pytest.raises(backup.BackupError, match="must be 1"):
        backup.BackupConfig.from_environment(environment)


def test_capture_only_also_requires_staging_encryption_acknowledgement(
    backup_environment: dict[str, str],
) -> None:
    environment = backup_environment.copy()
    environment["ILUVTRADE_BACKUP_STAGING_ENCRYPTED"] = "true"

    with pytest.raises(backup.BackupError, match="must be 1"):
        backup.BackupConfig.from_environment(environment, capture_only=True)


def test_backup_deployment_id_must_be_a_canonical_uuid(
    backup_environment: dict[str, str],
) -> None:
    environment = backup_environment.copy()
    environment["ILUVTRADE_BACKUP_DEPLOYMENT_ID"] = "shared"

    with pytest.raises(backup.BackupError, match="canonical UUID"):
        backup.BackupConfig.from_environment(environment)


def test_capture_only_configuration_does_not_require_remote_repository_credentials(
    backup_environment: dict[str, str],
) -> None:
    environment = backup_environment.copy()
    for name in (
        "RESTIC_REPOSITORY",
        "RESTIC_PASSWORD_FILE",
        "B2_ACCOUNT_ID",
        "B2_ACCOUNT_KEY",
        "ILUVTRADE_BACKUP_TAG",
    ):
        environment.pop(name)

    config = backup.BackupConfig.from_environment(environment, capture_only=True)

    assert config.restic_password_file is None
    assert config.restic_repository == ""


def test_capture_only_requires_unique_explicit_rehearsal_identity(
    backup_environment: dict[str, str],
) -> None:
    config = backup.BackupConfig.from_environment(
        backup_environment,
        capture_only=True,
    )

    with pytest.raises(backup.BackupError, match="unique lowercase UUID"):
        backup._rehearsal_config(
            config,
            {
                **backup_environment,
                "ILUVTRADE_BACKUP_REHEARSAL_PROJECT_NAME": "",
            },
        )

    with pytest.raises(backup.BackupError, match="unique lowercase UUID"):
        backup._rehearsal_config(
            config,
            {
                **backup_environment,
                "ILUVTRADE_BACKUP_REHEARSAL_PROJECT_NAME": "iluvtrade",
            },
        )

    with pytest.raises(backup.BackupError, match="ISOLATION_CONFIRMED"):
        backup._rehearsal_config(
            config,
            {
                **backup_environment,
                "ILUVTRADE_BACKUP_REHEARSAL_ISOLATION_CONFIRMED": "",
            },
        )


def test_capture_only_rejects_application_repository_as_project_directory(
    backup_environment: dict[str, str],
) -> None:
    environment = backup_environment.copy()
    environment["ILUVTRADE_BACKUP_PROJECT_DIR"] = str(backup._REPO_ROOT)
    environment["ILUVTRADE_BACKUP_COMPOSE_FILE"] = str(backup._REPO_ROOT / "docker-compose.yml")
    config = backup.BackupConfig.from_environment(environment, capture_only=True)

    with pytest.raises(backup.BackupError, match="separate from the application repository"):
        backup._rehearsal_config(config, environment)


def test_staging_inside_compose_project_is_rejected(
    backup_environment: dict[str, str],
) -> None:
    environment = backup_environment.copy()
    project_dir = Path(environment["ILUVTRADE_BACKUP_PROJECT_DIR"])
    unsafe_staging = project_dir / "staging"
    unsafe_staging.mkdir()
    environment["ILUVTRADE_BACKUP_STAGING_DIR"] = str(unsafe_staging)

    with pytest.raises(backup.BackupError, match="outside the Compose project"):
        backup.BackupConfig.from_environment(environment)


def test_staging_inside_application_mount_is_rejected(
    backup_environment: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = backup_environment.copy()
    mount = tmp_path / "application-data"
    unsafe_staging = mount / "staging"
    unsafe_staging.mkdir(parents=True)
    environment["ILUVTRADE_BACKUP_STAGING_DIR"] = str(unsafe_staging)
    config = backup.BackupConfig.from_environment(environment)
    monkeypatch.setattr(
        backup,
        "_container_mount_sources",
        lambda _config, _container_id: [mount],
    )

    with pytest.raises(backup.BackupError, match="overlaps an application data mount"):
        backup._ensure_staging_outside_mounts(config, ["mock-container"])


def test_backup_lock_rejects_an_overlapping_run(tmp_path: Path) -> None:
    with (
        backup._backup_lock(tmp_path / "staging"),
        pytest.raises(backup.BackupError, match="Another backup"),
        backup._backup_lock(tmp_path / "staging"),
    ):
        pytest.fail("second backup unexpectedly acquired the lock")


def test_storage_manifest_and_bundle_checksums_detect_changes(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    (storage / "dataset.csv").write_bytes(b"canonical-data")
    checksums = tmp_path / backup._CHECKSUMS_NAME
    digest, count = backup._storage_checksums(storage, checksums)
    archive = tmp_path / "storage.tar"
    backup._archive_storage(storage, archive)
    database = tmp_path / "database.dump"
    database.write_bytes(b"postgres-custom-dump")
    manifest = {
        "format_version": 1,
        "alembic_revision": "test-revision",
        "storage_file_count": count,
        "checksums": {
            "database.dump": backup._sha256_file(database),
            "storage.tar": backup._sha256_file(archive),
            backup._CHECKSUMS_NAME: digest,
        },
    }
    (tmp_path / backup._MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")

    assert count == 1
    assert backup._verify_bundle(tmp_path)["checksums"] == manifest["checksums"]

    archive.write_bytes(b"tampered")
    with pytest.raises(backup.BackupError, match=r"storage\.tar"):
        backup._verify_bundle(tmp_path)


def test_database_storage_references_match_manifest_and_recorded_hashes(tmp_path: Path) -> None:
    checksums = tmp_path / backup._CHECKSUMS_NAME
    recorded_hash = hashlib.sha256(b"raw-upload").hexdigest()
    checksums.write_text(
        json.dumps({"path": "org/1/raw/2.csv", "size_bytes": 10, "sha256": recorded_hash}) + "\n",
        encoding="utf-8",
    )
    references = tmp_path / "references.tsv"
    references.write_text(
        f"org/1/raw/2.csv\t{recorded_hash}\norg/1/raw/2.csv\t\n",
        encoding="utf-8",
    )

    assert backup._verify_storage_references(checksums, references) == (2, 1)

    references.write_text(f"org/1/raw/2.csv\t{'0' * 64}\n", encoding="utf-8")
    with pytest.raises(backup.BackupError, match="does not match"):
        backup._verify_storage_references(checksums, references)


def test_storage_checksum_manifest_rejects_symlinks(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    (storage / "link").symlink_to(tmp_path / "outside")

    with pytest.raises(backup.BackupError, match="symbolic link"):
        backup._storage_checksums(storage, tmp_path / "checksums.jsonl")


def test_capture_resumes_running_writers_after_failure(
    backup_environment: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = backup.BackupConfig.from_environment()
    calls: list[list[str]] = []
    running = {"db", "api"}
    writers_at_dump: list[set[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        if command[:2] == ["docker", "compose"]:
            if "config" in command:
                return _completed(b"db\napi\n")
            if "ps" in command:
                if "--quiet" in command:
                    return _completed(b"container-id\n")
                if "--status" in command:
                    return _completed(("\n".join(sorted(running)) + "\n").encode())
                return _completed(b"db\napi\n")
            if "stop" in command:
                running.discard("api")
                return _completed()
            if "start" in command:
                running.add("api")
                return _completed()
            if "psql" in " ".join(command):
                return _completed(b"0\n")
            if "pg_dump" in " ".join(command):
                writers_at_dump.append(set(running))
                return _completed(returncode=1)
        if command[:2] == ["docker", "inspect"]:
            return _completed(b"volume|iluvtrade_storage|/var/lib/docker/volumes/storage\n")
        if command[:2] == ["docker", "cp"]:
            destination = Path(command[-1])
            (destination / "dataset.csv").write_bytes(b"stored-data")
            return _completed()
        return _completed()

    monkeypatch.setattr(backup.subprocess, "run", fake_run)
    with pytest.raises(backup.BackupError, match="PostgreSQL consistent dump"):
        backup._capture_bundle(config)

    stop_index = next(index for index, call in enumerate(calls) if "stop" in call)
    dump_index = next(index for index, call in enumerate(calls) if "pg_dump" in " ".join(call))
    start_index = next(index for index, call in enumerate(calls) if "start" in call)
    assert stop_index < dump_index < start_index
    assert writers_at_dump == [{"db"}]
    assert running == {"db", "api"}
    assert not list(config.staging_dir.glob(".capture-*"))


def _mock_capture_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    config: backup.BackupConfig,
    *,
    fail_dump: bool = False,
    fail_start: bool = False,
    initially_running: set[str] | None = None,
    effective_compose_config: bytes = b'{"services":{},"volumes":{"storage":{}}}',
    compose_config_returncode: int = 0,
) -> tuple[list[list[str]], set[str]]:
    calls: list[list[str]] = []
    running = {"db", "api"} if initially_running is None else set(initially_running)
    project_name = config.compose_project_name
    assert project_name is not None
    project_volume = f"{project_name}_storage"
    project_root = config.project_dir

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        if command[:2] == ["docker", "compose"]:
            assert "--project-name" in command
            assert command[command.index("--project-name") + 1] == project_name
            if "config" in command:
                if "--format" in command:
                    return _completed(
                        effective_compose_config,
                        returncode=compose_config_returncode,
                    )
                return _completed(b"db\napi\n")
            if "ps" in command:
                if "--quiet" in command:
                    return _completed(
                        b"database-container\n" if command[-1] == "db" else b"storage-container\n"
                    )
                if "--status" in command:
                    return _completed(("\n".join(sorted(running)) + "\n").encode())
                return _completed(b"db\napi\n")
            if "stop" in command:
                running.discard("api")
                return _completed()
            if "start" in command:
                if fail_start:
                    return _completed(returncode=1)
                running.add("api")
                return _completed()
            if "pg_stat_activity" in " ".join(command):
                return _completed(b"0\n")
            if "version_num" in " ".join(command):
                return _completed(b"0008_temporal_evidence\n")
            if "pg_dump" in " ".join(command):
                if fail_dump:
                    return _completed(returncode=1)
                kwargs["stdout"].write(b"synthetic-postgresql-dump")
                return _completed()
        if command[:2] == ["docker", "inspect"]:
            template = command[3]
            if "com.docker.compose.project" in template:
                return _completed(
                    (
                        f"{project_name}\nvolume|{project_volume}|"
                        f"/var/lib/docker/volumes/{project_volume}/_data\n"
                    ).encode()
                )
            if "{{range .Mounts}}{{.Source}}" in template:
                return _completed(f"{project_root / 'volume-source'}\n".encode())
            if ".Destination" in template:
                return _completed(
                    (
                        f"volume|{project_volume}|/var/lib/docker/volumes/{project_volume}/_data\n"
                    ).encode()
                )
        if command[:3] == ["docker", "volume", "inspect"]:
            return _completed(f"{project_name}\n".encode())
        if command[:2] == ["docker", "cp"]:
            (Path(command[-1]) / "fixture.csv").write_bytes(b"synthetic fixture data")
            return _completed()
        pytest.fail(f"Unexpected mocked subprocess command: {command!r}")

    monkeypatch.setattr(backup.subprocess, "run", fake_run)
    return calls, running


@pytest.mark.parametrize(
    "effective_compose_config",
    [
        b'{"services":{},"volumes":{"storage":{"external":true}}}',
        b'{"services":{},"volumes":{"storage":{"external":{"name":"shared"}}}}',
        # `docker compose config` resolves interpolation before producing JSON.
        b'{"services":{},"volumes":{"storage":{"external":true,"name":"shared"}}}',
    ],
    ids=["external-boolean", "external-mapping", "interpolated-external"],
)
def test_capture_only_rejects_effective_external_volume_before_capture(
    backup_environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    effective_compose_config: bytes,
) -> None:
    config = backup.BackupConfig.from_environment(capture_only=True)
    rehearsal_config = backup._rehearsal_config(config, backup_environment)
    calls, _running = _mock_capture_subprocess(
        monkeypatch,
        rehearsal_config,
        effective_compose_config=effective_compose_config,
    )

    with pytest.raises(backup.BackupError, match="External Compose volumes are refused"):
        backup._capture_only(rehearsal_config)

    compose_config_call = calls[0]
    assert compose_config_call[compose_config_call.index("config") :] == [
        "config",
        "--format",
        "json",
    ]
    assert len(calls) == 1
    assert not any("stop" in call for call in calls)
    assert not any("pg_dump" in " ".join(call) or call[:2] == ["docker", "cp"] for call in calls)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    ("effective_compose_config", "returncode", "error"),
    [
        (b"not-json", 0, "could not be parsed"),
        (b"", 1, "configuration validation failed"),
    ],
    ids=["invalid-json", "compose-config-unavailable"],
)
def test_capture_only_fails_closed_on_unavailable_or_invalid_effective_config(
    backup_environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    effective_compose_config: bytes,
    returncode: int,
    error: str,
) -> None:
    config = backup.BackupConfig.from_environment(capture_only=True)
    rehearsal_config = backup._rehearsal_config(config, backup_environment)
    calls, _running = _mock_capture_subprocess(
        monkeypatch,
        rehearsal_config,
        effective_compose_config=effective_compose_config,
        compose_config_returncode=returncode,
    )

    with pytest.raises(backup.BackupError, match=error):
        backup._capture_only(rehearsal_config)

    assert len(calls) == 1
    assert not any("stop" in call for call in calls)
    assert not any("pg_dump" in " ".join(call) or call[:2] == ["docker", "cp"] for call in calls)


def test_capture_only_keeps_verified_bundle_without_restic_or_retention(
    backup_environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = backup.BackupConfig.from_environment(capture_only=True)
    rehearsal_config = backup._rehearsal_config(config, backup_environment)
    calls, running = _mock_capture_subprocess(monkeypatch, rehearsal_config)

    assert backup.main(["--capture-only"]) == 0
    message = capsys.readouterr().out
    assert "Local recovery bundle captured and verified:" in message
    bundle = Path(message.rsplit(":", 1)[1].strip())
    assert bundle.is_absolute()
    assert bundle.parent == config.staging_dir
    assert bundle.is_dir()
    manifest = backup._verify_bundle(bundle)
    assert manifest["storage_file_count"] == 1
    assert (bundle / "database.dump").read_bytes() == b"synthetic-postgresql-dump"
    assert running == {"db", "api"}
    assert not any(call[0] == "restic" for call in calls)
    assert not any("forget" in call or "--prune" in call for call in calls)
    assert not any(call[:2] == ["docker", "compose"] and "down" in call for call in calls)


def test_capture_only_does_not_start_services_that_were_already_stopped(
    backup_environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = backup.BackupConfig.from_environment(capture_only=True)
    rehearsal_config = backup._rehearsal_config(config, backup_environment)
    calls, running = _mock_capture_subprocess(
        monkeypatch,
        rehearsal_config,
        initially_running={"db"},
    )

    bundle = backup._capture_only(rehearsal_config)

    assert backup._verify_bundle(bundle)
    assert running == {"db"}
    assert not any("stop" in call or "start" in call for call in calls)


def test_capture_only_failure_keeps_partial_evidence_and_resumes_writers(
    backup_environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = backup.BackupConfig.from_environment(capture_only=True)
    rehearsal_config = backup._rehearsal_config(config, backup_environment)
    calls, running = _mock_capture_subprocess(
        monkeypatch,
        rehearsal_config,
        fail_dump=True,
    )

    assert backup.main(["--capture-only"]) == 1
    error = capsys.readouterr().err
    assert "Partial capture evidence retained at" in error
    retained = list(config.staging_dir.glob("failed-capture-*"))
    assert len(retained) == 1
    assert str(retained[0].resolve()) in error
    assert retained[0].is_dir()
    assert running == {"db", "api"}
    assert any("stop" in call for call in calls)
    assert any("start" in call for call in calls)
    assert not any(call[0] == "restic" for call in calls)


def test_capture_and_resume_failures_preserve_capture_error_and_partial_evidence(
    backup_environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = backup.BackupConfig.from_environment(capture_only=True)
    rehearsal_config = backup._rehearsal_config(config, backup_environment)
    calls, running = _mock_capture_subprocess(
        monkeypatch,
        rehearsal_config,
        fail_dump=True,
        fail_start=True,
    )

    assert backup.main(["--capture-only"]) == 1
    error = capsys.readouterr().err
    assert "PostgreSQL consistent dump failed" in error
    assert "Writer resumption also failed" in error
    assert "Partial capture evidence retained at" in error
    assert len(list(config.staging_dir.glob("failed-capture-*"))) == 1
    assert running == {"db"}
    assert any("start" in call for call in calls)


@pytest.mark.parametrize(
    ("inspect_output", "volume_label", "message"),
    [
        ("another-project\n", "another-project", "project label"),
        (
            "iluvtrade-rehearsal-1b2f8c6e-6c5a-4f7d-8a1b-3c4d5e6f7082\nbind||/production/storage\n",
            "",
            "bind mounts",
        ),
        (
            "iluvtrade-rehearsal-1b2f8c6e-6c5a-4f7d-8a1b-3c4d5e6f7082\n"
            "volume|shared-volume|/var/lib/docker/volumes/shared-volume/_data\n",
            "another-project",
            "non-project-scoped",
        ),
    ],
)
def test_capture_only_rejects_unowned_containers_and_volumes(
    backup_environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    inspect_output: str,
    volume_label: str,
    message: str,
) -> None:
    config = backup.BackupConfig.from_environment(capture_only=True)
    rehearsal_config = backup._rehearsal_config(config, backup_environment)
    monkeypatch.setattr(
        backup,
        "_service_container_ids",
        lambda _config, _service: ["container-id"],
    )

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if command[:2] == ["docker", "inspect"]:
            return _completed(inspect_output.encode())
        if command[:3] == ["docker", "volume", "inspect"]:
            return _completed(f"{volume_label}\n".encode())
        pytest.fail(f"Unexpected mocked subprocess command: {command!r}")

    monkeypatch.setattr(backup.subprocess, "run", fake_run)
    with pytest.raises(backup.BackupError, match=message):
        backup._validate_rehearsal_containers(rehearsal_config, {"api"})


def test_capture_only_rejects_volume_with_wrong_project_ownership_label(
    backup_environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = backup.BackupConfig.from_environment(capture_only=True)
    rehearsal_config = backup._rehearsal_config(config, backup_environment)
    project_name = rehearsal_config.compose_project_name
    assert project_name is not None
    monkeypatch.setattr(
        backup,
        "_service_container_ids",
        lambda _config, _service: ["container-id"],
    )

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if command[:2] == ["docker", "inspect"]:
            return _completed(
                (
                    f"{project_name}\nvolume|{project_name}_storage|"
                    "/var/lib/docker/volumes/storage/_data\n"
                ).encode()
            )
        if command[:3] == ["docker", "volume", "inspect"]:
            return _completed(b"another-project\n")
        pytest.fail(f"Unexpected mocked subprocess command: {command!r}")

    monkeypatch.setattr(backup.subprocess, "run", fake_run)
    with pytest.raises(backup.BackupError, match="not labeled as owned"):
        backup._validate_rehearsal_containers(rehearsal_config, {"api"})


def test_capture_creates_manifest_and_resumes_compose_writers(
    backup_environment: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = backup.BackupConfig.from_environment()
    running = {"db", "api"}
    calls: list[list[str]] = []
    writers_during_capture: list[set[str]] = []
    project_dir = Path(backup_environment["ILUVTRADE_BACKUP_PROJECT_DIR"])
    storage_source = project_dir / "storage-volume"
    database_source = project_dir / "db-volume"

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        if command[:2] == ["docker", "compose"]:
            if "config" in command:
                return _completed(b"db\napi\n")
            if "ps" in command:
                if "--quiet" in command:
                    return _completed(
                        b"database-container\n" if command[-1] == "db" else b"storage-container\n"
                    )
                if "--status" in command:
                    return _completed(("\n".join(sorted(running)) + "\n").encode())
                return _completed(b"db\napi\n")
            if "stop" in command:
                running.discard("api")
                return _completed()
            if "start" in command:
                running.add("api")
                return _completed()
            if "pg_stat_activity" in " ".join(command):
                return _completed(b"0\n")
            if "version_num" in " ".join(command):
                return _completed(b"0008_temporal_evidence\n")
            if "pg_dump" in " ".join(command):
                writers_during_capture.append(set(running))
                kwargs["stdout"].write(b"custom-format-dump")
                return _completed()
        if command[:2] == ["docker", "inspect"]:
            template = command[3]
            if "/var/lib/iluvtrade/storage" in template:
                return _completed(b"volume|storage|/var/lib/docker/storage\n")
            source = database_source if command[-1] == "database-container" else storage_source
            return _completed(f"{source}\n".encode())
        if command[:2] == ["docker", "cp"]:
            writers_during_capture.append(set(running))
            (Path(command[-1]) / "raw.csv").write_bytes(b"dataset bytes")
            return _completed()
        pytest.fail(f"Unexpected mocked subprocess command: {command!r}")

    monkeypatch.setattr(backup.subprocess, "run", fake_run)
    bundle = backup._capture_bundle(config)
    manifest = backup._verify_bundle(bundle)

    assert running == {"db", "api"}
    assert manifest["application"] == {"version": "0.1.0", "commit": "a" * 40}
    assert manifest["alembic_revision"] == "0008_temporal_evidence"
    assert manifest["deployment"]["id"] == config.deployment_id
    assert manifest["storage_file_count"] == 1
    assert (bundle / "database.dump").read_bytes() == b"custom-format-dump"
    assert (bundle / "storage.tar").is_file()
    stop_index = next(index for index, call in enumerate(calls) if "stop" in call)
    dump_index = next(index for index, call in enumerate(calls) if "pg_dump" in " ".join(call))
    storage_index = next(index for index, call in enumerate(calls) if call[:2] == ["docker", "cp"])
    start_index = next(index for index, call in enumerate(calls) if "start" in call)
    assert stop_index < dump_index < storage_index < start_index
    assert writers_during_capture == [{"db"}, {"db"}]


def test_failed_upload_keeps_local_bundle_and_never_forgets(
    backup_environment: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = backup.BackupConfig.from_environment()
    bundle = Path(backup_environment["ILUVTRADE_BACKUP_STAGING_DIR"]) / "recovery-pending"
    bundle.mkdir()
    _write_valid_bundle(bundle)
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        if "backup" in command:
            return _completed(returncode=1)
        return _completed(b"[]\n")

    monkeypatch.setattr(backup.subprocess, "run", fake_run)
    with pytest.raises(backup.BackupError, match="retained"):
        backup._upload_pending(config, bundle)

    assert bundle.is_dir()
    assert not any("forget" in call for call in calls)


def test_pending_bundle_symlink_is_rejected(
    backup_environment: dict[str, str], tmp_path: Path
) -> None:
    config = backup.BackupConfig.from_environment()
    bundle = Path(backup_environment["ILUVTRADE_BACKUP_STAGING_DIR"]) / "recovery-pending"
    bundle.mkdir()
    _write_valid_bundle(bundle)
    link = Path(backup_environment["ILUVTRADE_BACKUP_STAGING_DIR"]) / "recovery-link"
    link.symlink_to(bundle, target_is_directory=True)

    with pytest.raises(backup.BackupError, match="symbolic links"):
        backup._upload_pending(config, link)

    assert bundle.is_dir()


def test_retention_failure_keeps_local_bundle(
    backup_environment: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = backup.BackupConfig.from_environment()
    bundle = Path(backup_environment["ILUVTRADE_BACKUP_STAGING_DIR"]) / "recovery-pending"
    bundle.mkdir()
    _write_valid_bundle(bundle)
    snapshot_id = "1234567890abcdef" * 4
    last_known_good_id = "fedcba0987654321" * 4
    calls: list[list[str]] = []
    observed_snapshots: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        if "backup" in command:
            return _completed(
                (
                    json.dumps({"message_type": "summary", "snapshot_id": snapshot_id}) + "\n"
                ).encode()
            )
        if "snapshots" in command:
            observed_snapshots.append([snapshot_id, last_known_good_id])
            return _completed(
                json.dumps(
                    [
                        {
                            "id": current_id,
                            "tags": [config.backup_tag, config.deployment_tag],
                        }
                        for current_id in (snapshot_id, last_known_good_id)
                    ]
                ).encode()
            )
        if "forget" in command:
            return _completed(returncode=1)
        return _completed()

    monkeypatch.setattr(backup.subprocess, "run", fake_run)
    with pytest.raises(backup.BackupError, match="retained"):
        backup._upload_pending(config, bundle)
    assert bundle.is_dir()
    forget_index = next(index for index, call in enumerate(calls) if "forget" in call)
    verified_snapshot_index = next(
        index for index in range(forget_index) if "snapshots" in calls[index]
    )
    assert verified_snapshot_index < forget_index
    assert observed_snapshots[1] == [snapshot_id, last_known_good_id]
    assert calls[forget_index][calls[forget_index].index("--keep-last") + 1] == "2"


def test_unverified_snapshot_skips_retention_and_preserves_local_bundle(
    backup_environment: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = backup.BackupConfig.from_environment()
    bundle = Path(backup_environment["ILUVTRADE_BACKUP_STAGING_DIR"]) / "recovery-unverified"
    bundle.mkdir()
    _write_valid_bundle(bundle)
    snapshot_id = "1234567890abcdef" * 4
    last_known_good_id = "fedcba0987654321" * 4
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        if "backup" in command:
            return _completed(
                (
                    json.dumps({"message_type": "summary", "snapshot_id": snapshot_id}) + "\n"
                ).encode()
            )
        if "snapshots" in command:
            return _completed(
                json.dumps(
                    [
                        {
                            "id": last_known_good_id,
                            "tags": [config.backup_tag, config.deployment_tag],
                        }
                    ]
                ).encode()
            )
        return _completed()

    monkeypatch.setattr(backup.subprocess, "run", fake_run)
    with pytest.raises(backup.BackupError, match="retention was not applied"):
        backup._upload_pending(config, bundle)

    assert bundle.is_dir()
    assert not any("forget" in call for call in calls)


def test_retention_refuses_snapshots_with_mismatched_deployment_tags(
    backup_environment: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = backup.BackupConfig.from_environment()
    bundle = Path(backup_environment["ILUVTRADE_BACKUP_STAGING_DIR"]) / "recovery-foreign"
    bundle.mkdir()
    _write_valid_bundle(bundle)
    snapshot_id = "1234567890abcdef" * 4
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        if "backup" in command:
            return _completed(
                (
                    json.dumps({"message_type": "summary", "snapshot_id": snapshot_id}) + "\n"
                ).encode()
            )
        if "snapshots" in command:
            return _completed(
                json.dumps(
                    [
                        {
                            "id": snapshot_id,
                            "tags": [config.backup_tag, "deployment-other-uuid"],
                        }
                    ]
                ).encode()
            )
        return _completed()

    monkeypatch.setattr(backup.subprocess, "run", fake_run)
    with pytest.raises(backup.BackupError, match="unexpected ownership tags"):
        backup._upload_pending(config, bundle)

    assert bundle.is_dir()
    assert not any("forget" in call for call in calls)


def _write_valid_bundle(bundle: Path) -> None:
    database = bundle / "database.dump"
    database.write_bytes(b"database")
    archive = bundle / "storage.tar"
    with tarfile.open(archive, "w"):
        pass
    checksum_list = bundle / backup._CHECKSUMS_NAME
    checksum_list.write_text("", encoding="utf-8")
    manifest = {
        "format_version": 1,
        "alembic_revision": "0008_temporal_evidence",
        "storage_file_count": 0,
        "checksums": {
            "database.dump": backup._sha256_file(database),
            "storage.tar": backup._sha256_file(archive),
            backup._CHECKSUMS_NAME: backup._sha256_file(checksum_list),
        },
    }
    (bundle / backup._MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")


def test_success_verifies_snapshot_before_retention_and_cleanup(
    backup_environment: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = backup.BackupConfig.from_environment()
    bundle = Path(backup_environment["ILUVTRADE_BACKUP_STAGING_DIR"]) / "recovery-good"
    bundle.mkdir()
    _write_valid_bundle(bundle)
    snapshot_id = "0123456789abcdef" * 4
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        if "backup" in command:
            return _completed(
                (
                    json.dumps({"message_type": "summary", "snapshot_id": snapshot_id}) + "\n"
                ).encode()
            )
        if "snapshots" in command:
            return _completed(
                json.dumps(
                    [
                        {
                            "id": snapshot_id,
                            "tags": [config.backup_tag, config.deployment_tag],
                        }
                    ]
                ).encode()
            )
        return _completed()

    monkeypatch.setattr(backup.subprocess, "run", fake_run)
    assert backup._upload_pending(config, bundle) == snapshot_id
    assert not bundle.exists()

    backup_index = next(index for index, call in enumerate(calls) if "backup" in call)
    forget_index = next(index for index, call in enumerate(calls) if "forget" in call)
    assert backup_index < forget_index
    assert "--keep-daily" in calls[forget_index]
    assert calls[forget_index][calls[forget_index].index("--keep-daily") + 1] == "7"
    assert "--keep-weekly" in calls[forget_index]
    assert "--keep-monthly" in calls[forget_index]
    assert "--keep-last" in calls[forget_index]
    snapshot_calls = [index for index, call in enumerate(calls) if "snapshots" in call]
    assert len(snapshot_calls) == 3
    assert snapshot_calls[0] < backup_index < snapshot_calls[1] < forget_index < snapshot_calls[2]
    assert calls[forget_index][calls[forget_index].index("--tag") + 1] == config.deployment_tag
    assert calls[backup_index].count("--tag") == 2


def test_restic_secret_environment_is_not_part_of_command(
    backup_environment: dict[str, str],
) -> None:
    config = backup.BackupConfig.from_environment()
    command = ["restic", "snapshots", "--json"]
    environment = backup._restic_environment(config)

    assert backup_environment["B2_ACCOUNT_KEY"] not in command
    assert environment["RESTIC_PASSWORD_FILE"] == backup_environment["RESTIC_PASSWORD_FILE"]
    assert stat.S_IMODE(Path(environment["RESTIC_PASSWORD_FILE"]).stat().st_mode) == 0o600


def test_bundle_verification_cli_does_not_need_external_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _write_valid_bundle(bundle)
    monkeypatch.setattr(
        backup.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("bundle verification must remain offline"),
    )

    assert backup.main(["--verify-bundle", str(bundle)]) == 0
    assert "Local recovery bundle verified" in capsys.readouterr().out


def test_storage_archive_hash_changes_when_payload_changes(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    payload = storage / "file.csv"
    payload.write_text("one", encoding="utf-8")
    first = tmp_path / "first.tar"
    backup._archive_storage(storage, first)
    first_digest = hashlib.sha256(first.read_bytes()).hexdigest()
    payload.write_text("two", encoding="utf-8")
    second = tmp_path / "second.tar"
    backup._archive_storage(storage, second)

    assert hashlib.sha256(second.read_bytes()).hexdigest() != first_digest
