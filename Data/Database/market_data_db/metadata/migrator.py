from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Engine, inspect, text

from .models import Base

# Ordered list of (version, upgrade_sql_statements). Version 1 is the baseline
# schema, created via Base.metadata.create_all. Future schema changes append new
# (version, [sql, ...]) entries here rather than mutating existing ones.
MIGRATIONS: list[tuple[int, list[str]]] = [
    (1, []),  # baseline: tables created from ORM metadata below
]


def current_version(engine: Engine) -> int:
    inspector = inspect(engine)
    if "schema_version" not in inspector.get_table_names():
        return 0
    with engine.connect() as conn:
        row = conn.execute(text("SELECT MAX(version) FROM schema_version")).scalar()
        return row or 0


def run_migrations(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    version = current_version(engine)
    with engine.begin() as conn:
        for target_version, statements in MIGRATIONS:
            if target_version <= version:
                continue
            for stmt in statements:
                conn.execute(text(stmt))
            conn.execute(
                text("INSERT INTO schema_version (version, applied_at) VALUES (:v, :t)"),
                {"v": target_version, "t": datetime.now(timezone.utc)},
            )
