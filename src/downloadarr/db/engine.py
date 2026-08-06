from pathlib import Path

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .migrations import MIGRATIONS


class Database:
    def __init__(self, url: str) -> None:
        parsed = make_url(url)
        if parsed.drivername.startswith("sqlite") and parsed.database not in (None, "", ":memory:"):
            Path(parsed.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self.engine: AsyncEngine = create_async_engine(url)
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
            for version, script in sorted(MIGRATIONS.items()):
                if version in versions:
                    continue
                for statement in script.split(";"):
                    if statement.strip():
                        await connection.exec_driver_sql(statement)
                await connection.exec_driver_sql(
                    "INSERT INTO downloadarr_schema_version(version) VALUES (?)", (version,))

    def session(self) -> AsyncSession:
        return self.sessions()

    async def close(self) -> None:
        await self.engine.dispose()
