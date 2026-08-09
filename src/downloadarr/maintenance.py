import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy.engine import make_url

from .db.migrations import MIGRATIONS
from .durability import fsync_directory
from .process_lock import ProcessLock, ProcessLockError
from .settings import Settings, load_settings, settings_path


def _database_path(settings: Settings) -> Path:
    parsed = make_url(settings.database_url)
    if not parsed.drivername.startswith("sqlite") or parsed.database in (None, "", ":memory:"):
        raise ValueError("maintenance requires a file-backed SQLite database")
    return Path(parsed.database).expanduser().resolve()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _app_version() -> str:
    try:
        return version("downloadarr")
    except PackageNotFoundError:
        return os.environ.get("DOWNLOADARR_VERSION", "0.1.0")


def _integrity(path: Path) -> int:
    with closing(sqlite3.connect(path)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("backup database integrity check failed")
        row = connection.execute(
            "SELECT MAX(version) FROM downloadarr_schema_version").fetchone()
        schema = int(row[0] or 0)
        if schema > max(MIGRATIONS):
            raise ValueError("backup schema is newer than this Downloadarr version")
        return schema


def create_backup(output: Path, config: Path | None = None) -> Path:
    configured_path = settings_path(config).expanduser().resolve()
    settings = load_settings(configured_path)
    database = _database_path(settings)
    output = output.expanduser().resolve()
    if output.exists():
        raise ValueError("backup destination already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=output.name + ".tmp-", dir=output.parent))
    try:
        if os.name != "nt":
            os.chmod(temporary, 0o700)
        snapshot = temporary / "downloadarr.db"
        with closing(sqlite3.connect(database)) as source, closing(sqlite3.connect(snapshot)) as target:
            source.backup(target)
        schema = _integrity(snapshot)
        copied_settings = temporary / "settings.json"
        shutil.copy2(configured_path, copied_settings)
        Settings.model_validate_json(copied_settings.read_text(encoding="utf-8"))
        for item in (snapshot, copied_settings):
            if os.name != "nt":
                os.chmod(item, 0o600)
        manifest = {
            "format_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "downloadarr_version": _app_version(),
            "schema_version": schema,
            "scope": {"database": True, "settings": True, "media_staging": False},
            "warning": "Settings may contain secrets; media partials/manifests are not included.",
            "files": {"downloadarr.db": _sha256(snapshot),
                      "settings.json": _sha256(copied_settings)},
        }
        manifest_path = temporary / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        if os.name != "nt":
            os.chmod(manifest_path, 0o600)
        for item in (snapshot, copied_settings, manifest_path):
            with item.open("r+b") as handle:
                os.fsync(handle.fileno())
        fsync_directory(temporary)
        try:
            os.rename(temporary, output)
        except PermissionError:
            if os.name != "nt":
                raise
            output.mkdir()
            for item in temporary.iterdir():
                os.replace(item, output / item.name)
            temporary.rmdir()
        fsync_directory(output.parent)
        return output
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def verify_backup(bundle: Path) -> dict:
    bundle = bundle.expanduser().resolve()
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format_version") != 1 or not isinstance(manifest.get("files"), dict):
        raise ValueError("unsupported backup manifest")
    for name, expected in manifest["files"].items():
        if name not in {"downloadarr.db", "settings.json"}:
            raise ValueError("backup manifest contains an unexpected file")
        path = bundle / name
        if not path.is_file() or _sha256(path) != expected:
            raise ValueError(f"backup checksum failed: {name}")
    Settings.model_validate_json((bundle / "settings.json").read_text(encoding="utf-8"))
    schema = _integrity(bundle / "downloadarr.db")
    return {"status": "ok", "format_version": 1, "schema_version": schema,
            "created_at": manifest.get("created_at"), "scope": manifest.get("scope")}


def restore_backup(bundle: Path, config: Path | None, confirmation: str) -> Path:
    if confirmation != "RESTORE":
        raise ValueError("restore requires --confirm RESTORE")
    verified = verify_backup(bundle)
    configured_path = settings_path(config).expanduser().resolve()
    current = load_settings(configured_path)
    database = _database_path(current)
    lock = ProcessLock.for_database(current.database_url)
    lock.acquire()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    recovery = configured_path.parent / f"pre-restore-{stamp}"
    try:
        create_backup(recovery, configured_path)
        database.parent.mkdir(parents=True, exist_ok=True)
        db_temp = database.with_name(database.name + ".restore.tmp")
        settings_temp = configured_path.with_name(configured_path.name + ".restore.tmp")
        shutil.copy2(bundle / "downloadarr.db", db_temp)
        shutil.copy2(bundle / "settings.json", settings_temp)
        _integrity(db_temp)
        os.replace(db_temp, database)
        os.replace(settings_temp, configured_path)
        for suffix in ("-wal", "-shm"):
            database.with_name(database.name + suffix).unlink(missing_ok=True)
        fsync_directory(database.parent)
        return recovery
    finally:
        lock.release()


def doctor(config: Path | None = None, online: bool = False) -> dict:
    configured_path = settings_path(config).expanduser().resolve()
    results = []
    try:
        settings = load_settings(configured_path)
        database = _database_path(settings)
        results.append(_result("settings", "pass", "Settings schema is valid"))
    except Exception as error:
        return {"status": "fail", "checks": [_result("settings", "fail", str(error))]}
    if settings.qbittorrent.username == "admin" or settings.qbittorrent.password.get_secret_value() in {
            "admin", "adminadmin", "password"}:
        results.append(_result("credentials", "fail", "Default or weak qBittorrent credentials"))
    else:
        results.append(_result("credentials", "pass", "qBittorrent credentials are customized"))
    try:
        _integrity(database)
        results.append(_result("database", "pass", "SQLite integrity and schema are valid"))
    except Exception as error:
        results.append(_result("database", "fail", str(error)))
    filesystem = _filesystem_type(database)
    normalized_fs = filesystem.lower()
    level = ("fail" if normalized_fs.startswith(("nfs", "cifs", "smb", "fuse")) else
             "warn" if normalized_fs.startswith(("9p", "overlay")) else "pass")
    results.append(_result("config_filesystem", level,
                           f"SQLite filesystem type: {filesystem}"))
    for label, path in [("config", configured_path.parent), ("media", Path(settings.download.path))]:
        try:
            _write_probe(path)
            free = shutil.disk_usage(path).free
            results.append(_result(label + "_storage", "pass" if free >= 1024**3 else "warn",
                                   f"Atomic write/fsync works; free bytes={free}"))
        except Exception as error:
            results.append(_result(label + "_storage", "fail", str(error)))
    if online:
        hosts = {urlsplit(settings.torbox.api_base).hostname}
        hosts.update(urlsplit(item.url).hostname for item in (
            settings.integrations.sonarr, settings.integrations.radarr) if item.url)
        for host in sorted(value for value in hosts if value):
            try:
                socket.getaddrinfo(host, 443)
                results.append(_result("dns", "pass", f"DNS resolves {host}"))
            except OSError:
                results.append(_result("dns", "fail", f"DNS failed for {host}"))
    status = "fail" if any(item["level"] == "fail" for item in results) else (
        "warn" if any(item["level"] == "warn" for item in results) else "pass")
    return {"status": status, "checks": results}


def _write_probe(root: Path) -> None:
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".downloadarr-doctor-", dir=root) as directory:
        source = Path(directory) / "probe.tmp"
        target = Path(directory) / "probe.ok"
        with source.open("wb") as handle:
            handle.write(b"downloadarr")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(source, target)
        fsync_directory(Path(directory))


def _filesystem_type(path: Path) -> str:
    if os.name == "nt":
        return "windows-local-or-managed"
    resolved = path.resolve()
    best = (0, "unknown")
    try:
        for line in Path("/proc/mounts").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 3:
                mount = Path(parts[1].replace("\\040", " "))
                try:
                    resolved.relative_to(mount)
                except ValueError:
                    continue
                if len(str(mount)) > best[0]:
                    best = (len(str(mount)), parts[2])
    except OSError:
        pass
    return best[1]


def _result(name: str, level: str, message: str) -> dict:
    return {"name": name, "level": level, "message": message[:512]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="downloadarr-maintenance")
    parser.add_argument("--config", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    backup = sub.add_parser("backup")
    backup.add_argument("output", type=Path)
    verify = sub.add_parser("verify")
    verify.add_argument("bundle", type=Path)
    restore = sub.add_parser("restore")
    restore.add_argument("bundle", type=Path)
    restore.add_argument("--confirm", default="")
    doctor_parser = sub.add_parser("doctor")
    doctor_parser.add_argument("--online", action="store_true")
    doctor_parser.add_argument("--json", action="store_true",
                               help="JSON is the default stable output format")
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            result = {"status": "ok", "path": str(create_backup(args.output, args.config))}
        elif args.command == "verify":
            result = verify_backup(args.bundle)
        elif args.command == "restore":
            result = {"status": "ok", "recovery_backup": str(
                restore_backup(args.bundle, args.config, args.confirm))}
        else:
            result = doctor(args.config, args.online)
        print(json.dumps(result, separators=(",", ":")))
        return 0 if result.get("status") in {"ok", "pass", "warn"} else 1
    except (OSError, ValueError, ProcessLockError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "fail", "error": str(error)[:512]},
                         separators=(",", ":")), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
