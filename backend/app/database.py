from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.config import settings

connect_args: dict[str, object] = {}
engine_kwargs: dict[str, object] = {"pool_pre_ping": True}

if settings.database_url.startswith("sqlite"):
    connect_args["check_same_thread"] = False
    engine_kwargs["poolclass"] = StaticPool

engine = create_engine(settings.database_url, connect_args=connect_args, **engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
