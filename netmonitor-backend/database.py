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

    Supabase notes:
    - SSL is required; we pass ssl=True (asyncpg 0.29+ accepts True or an
      ssl.SSLContext; "require" string works in some versions but not all).
    - The Transaction pooler (port 6543) does not support server-side
      prepared statements, so statement_cache_size must be 0.
    """
    import ssl as _ssl
    from sqlalchemy.pool import NullPool

    url = os.environ["DATABASE_URL"]
    connect_args: dict = {}
    if "supabase.co" in url:
        if "ssl=" not in url:
            # Supabase's PgBouncer uses a self-signed cert that Vercel's CA
            # store doesn't trust.  We still want encryption, just without
            # certificate chain verification (standard practice for managed
            # Postgres poolers).
            ctx = _ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE
            connect_args["ssl"] = ctx
        # PgBouncer Transaction pooler (port 6543) does not support prepared
        # statements.  Setting statement_cache_size=0 disables asyncpg's
        # client-side prepared-statement cache so every query is sent as
        # a simple (unprepared) query, which the pooler can handle.
        connect_args["statement_cache_size"] = 0
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
