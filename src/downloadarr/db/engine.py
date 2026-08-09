from pathlib import Path

from sqlalchemy.engine import make_url
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .migrations import MIGRATIONS


class Database:
    def __init__(self, url: str) -> None:
        parsed = make_url(url)
        if parsed.drivername.startswith("sqlite") and parsed.database not in (None, "", ":memory:"):
            Path(parsed.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self.engine: AsyncEngine = create_async_engine(url)
        if parsed.drivername.startswith("sqlite"):
            @event.listens_for(self.engine.sync_engine, "connect")
            def _sqlite_pragmas(connection, _record) -> None:
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=5000")
                if parsed.database not in (None, "", ":memory:"):
                    cursor.execute("PRAGMA journal_mode=WAL")
                    cursor.execute("PRAGMA synchronous=FULL")
                    cursor.execute("PRAGMA wal_autocheckpoint=1000")
                cursor.close()
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def migrate(self) -> None:
        # Versioned metadata migration runner. Revision 1 is the initial schema.
        async with self.engine.begin() as connection:
            await connection.exec_driver_sql(
                "CREATE TABLE IF NOT EXISTS downloadarr_schema_version "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            rows = await connection.exec_driver_sql("SELECT version FROM downloadarr_schema_version")
            versions = {row[0] for row in rows}
            expected = max(MIGRATIONS)
            if versions and max(versions) > expected:
                raise RuntimeError("database schema is newer than this Downloadarr image")
            for version, script in sorted(MIGRATIONS.items()):
                if version in versions:
                    continue
                for statement in script.split(";"):
                    if statement.strip():
                        await connection.exec_driver_sql(statement)
                await connection.exec_driver_sql(
                    "INSERT INTO downloadarr_schema_version(version) VALUES (?)", (version,))
            check = await connection.exec_driver_sql("PRAGMA quick_check")
            if check.scalar() != "ok":
                raise RuntimeError("SQLite quick_check failed")

    async def readiness(self) -> bool:
        async with self.engine.connect() as connection:
            if (await connection.execute(text("SELECT 1"))).scalar() != 1:
                return False
            version = (await connection.exec_driver_sql(
                "SELECT MAX(version) FROM downloadarr_schema_version")).scalar()
            return version == max(MIGRATIONS)

    async def pragmas(self) -> dict[str, object]:
        result = {}
        async with self.engine.connect() as connection:
            for name in ("foreign_keys", "busy_timeout", "journal_mode",
                         "synchronous", "wal_autocheckpoint"):
                result[name] = (await connection.exec_driver_sql(f"PRAGMA {name}")).scalar()
        return result

    def session(self) -> AsyncSession:
        return self.sessions()

    async def close(self) -> None:
        try:
            async with self.engine.connect() as connection:
                await connection.exec_driver_sql("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception:
            pass
        await self.engine.dispose()
