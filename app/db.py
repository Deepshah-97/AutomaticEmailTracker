from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None


def init_engine(url: str) -> Engine:
    global _engine, _factory
    _engine = create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)
    _factory = sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("Database not initialised")
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    """Short transaction: commit on success, roll back on error. Never hold one across network calls."""
    if _factory is None:
        raise RuntimeError("Database not initialised")
    s = _factory()
    try:
        yield s
        s.commit()
    except BaseException:
        s.rollback()
        raise
    finally:
        s.close()


def get_db() -> Iterator[Session]:
    with session_scope() as s:
        yield s
