"""
database.py — SpanGate Network Monitor Backend
Async SQLAlchemy engine and session factory.

Connection string is read from the DATABASE_URL environment variable.
Expected format: postgresql+asyncpg://user:password@host:port/dbname
"""

import os

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

load_dotenv()


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""
    pass


def _build_engine():
    """
    Build the async SQLAlchemy engine from environment config.

    Uses NullPool so each serverless function invocation opens and closes
    its own connection rather than holding a pool between requests.
    Supabase's PgBouncer pooler handles connection reuse at the infra level.

    Supabase requires SSL on all connections.  asyncpg accepts ssl="require"
    via connect_args; this is added automatically when the URL contains
    supabase.co.  You can also append ?ssl=require to DATABASE_URL manually.
    """
    from sqlalchemy.pool import NullPool
    url = os.environ["DATABASE_URL"]
    connect_args: dict = {}
    if "supabase.co" in url and "ssl=" not in url:
        connect_args["ssl"] = "require"
    return create_async_engine(url, echo=False, poolclass=NullPool, connect_args=connect_args)


engine = _build_engine()

AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine,
    expire_on_commit=False,
)


async def get_db() -> AsyncSession:
    """
    FastAPI dependency that yields a database session and closes it when done.

    Usage in a route::

        async def my_route(db: AsyncSession = Depends(get_db)):
            ...
    """
    async with AsyncSessionLocal() as session:
        yield session
