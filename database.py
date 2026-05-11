"""
database.py
Gerencia o pool de conexões com o MikeDB (PostgreSQL).
"""

import asyncpg
from contextlib import asynccontextmanager

DSN = "postgresql://postgres:mikedb0702@localhost:5432/mikedb"

_pool: asyncpg.Pool | None = None


async def init_pool():
    global _pool
    _pool = await asyncpg.create_pool(DSN, min_size=2, max_size=10)


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Pool não inicializado. Chame init_pool() primeiro.")
    return _pool


@asynccontextmanager
async def db():
    """Context manager para adquirir conexão do pool."""
    pool = get_pool()
    async with pool.acquire() as conn:
        yield conn
