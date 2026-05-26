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
    """Build the async SQLAlchemy engine from environment config."""
    url = os.environ["DATABASE_URL"]
    return create_async_engine(
        url,
        echo=False,
        pool_pre_ping=True,   # discard stale connections before use
        pool_size=5,
        max_overflow=10,
    )


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
