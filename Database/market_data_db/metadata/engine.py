from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import DBConfig
from .migrator import run_migrations


def make_engine(config: DBConfig) -> Engine:
    config.ensure_dirs()
    connect_args = {}
    if config.metadata_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    engine = create_engine(config.metadata_url, connect_args=connect_args, future=True)
    run_migrations(engine)
    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)
