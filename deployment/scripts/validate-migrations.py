#!/usr/bin/env python3
"""Validate the market-data-db schema migration chain.

A schema migration is the one deployment step that can destroy data that no
image rollback brings back: rolling the container image back does not
un-drop a column. This gate therefore checks the migration chain itself,
before any image is built or deployed.

Four classes of check:

  structure   The MIGRATIONS list is well formed -- versions start at 1,
              increase strictly, and are contiguous. A gap or a duplicate
              makes `current_version` comparisons meaningless.

  append-only Versions that already exist on the base revision are
              byte-identical here. Editing a released migration means
              deployed databases and fresh ones diverge permanently, because
              the edited statement never re-runs where the version is
              already recorded.

  destructive No migration statement drops or truncates data. This is the
              schema-level half of the persistent-data protection
              requirement; the Terraform free-tier gate covers the volume
              and the Kubernetes validator covers the reclaim policy.

  executable  The chain actually runs: a fresh database reaches head, a
              second run is a no-op, every historical version upgrades to
              head, and the resulting schema matches the ORM metadata.

Structure, append-only and destructive checks are pure text analysis and run
with `--static-only` on any machine. The executable checks need sqlalchemy
and the market_data_db package importable.

Exit status is 0 only if every selected check passes.
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import re
import subprocess
import sys

# Statements that can lose committed market data or metadata. Matched on the
# normalised statement text. ALTER ... DROP COLUMN is listed separately
# because the bare "DROP" prefix would not catch it.
DESTRUCTIVE_PATTERNS: list[tuple[str, str]] = [
    (r"\bDROP\s+TABLE\b", "drops a table"),
    (r"\bDROP\s+DATABASE\b", "drops the database"),
    (r"\bDROP\s+SCHEMA\b", "drops a schema"),
    (r"\bDROP\s+COLUMN\b", "drops a column"),
    (r"\bTRUNCATE\b", "truncates a table"),
    (r"\bDELETE\s+FROM\b", "deletes rows"),
    # A rename is not destructive in itself, but it breaks every reader
    # pinned to the old name, including a rolled-back previous image.
    (r"\bRENAME\s+TO\b", "renames an object, which breaks a rolled-back image"),
]

MIGRATOR_REL = "Data/Database/market_data_db/metadata/migrator.py"


class Failure(Exception):
    """A check failed in a way that must stop the pipeline."""


def _parse_migrations(source: str, origin: str) -> list[tuple[int, list[str]]]:
    """Extract the MIGRATIONS literal from migrator.py source.

    Parsed with `ast` rather than imported: this must work on a source file
    taken from another git revision, whose imports may not resolve, and it
    must never execute code from the revision under test.
    """
    tree = ast.parse(source, filename=origin)
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            continue
        if value is None:
            continue
        if not any(isinstance(t, ast.Name) and t.id == "MIGRATIONS" for t in targets):
            continue
        try:
            literal = ast.literal_eval(value)
        except ValueError as exc:
            raise Failure(f"{origin}: MIGRATIONS is not a literal: {exc}") from exc
        return _coerce(literal, origin)
    raise Failure(f"{origin}: no MIGRATIONS assignment found")


def _coerce(literal: object, origin: str) -> list[tuple[int, list[str]]]:
    if not isinstance(literal, list):
        raise Failure(f"{origin}: MIGRATIONS must be a list, got {type(literal).__name__}")
    out: list[tuple[int, list[str]]] = []
    for index, entry in enumerate(literal):
        if not isinstance(entry, (tuple, list)) or len(entry) != 2:
            raise Failure(f"{origin}: MIGRATIONS[{index}] must be a (version, statements) pair")
        version, statements = entry
        if not isinstance(version, int) or isinstance(version, bool):
            raise Failure(f"{origin}: MIGRATIONS[{index}] version must be an int, got {version!r}")
        if not isinstance(statements, (list, tuple)) or not all(
            isinstance(s, str) for s in statements
        ):
            raise Failure(
                f"{origin}: migration {version} statements must be a list of strings"
            )
        out.append((version, list(statements)))
    return out


def check_structure(migrations: list[tuple[int, list[str]]]) -> list[str]:
    problems: list[str] = []
    if not migrations:
        return ["MIGRATIONS is empty; version 1 must at least declare the baseline"]

    versions = [v for v, _ in migrations]
    if versions[0] != 1:
        problems.append(f"the first migration must be version 1, found {versions[0]}")

    for previous, current in zip(versions, versions[1:]):
        if current <= previous:
            problems.append(
                f"versions must increase strictly: {current} follows {previous}"
            )
        elif current != previous + 1:
            problems.append(
                f"version gap between {previous} and {current}; versions must be contiguous "
                "so an interrupted upgrade resumes predictably"
            )

    duplicates = {v for v in versions if versions.count(v) > 1}
    for version in sorted(duplicates):
        problems.append(f"version {version} is declared more than once")

    return problems


def check_destructive(migrations: list[tuple[int, list[str]]]) -> list[str]:
    problems: list[str] = []
    for version, statements in migrations:
        for index, statement in enumerate(statements):
            # Collapse whitespace so a statement broken across lines still
            # matches, and strip -- comments so a mention in prose is not a
            # finding.
            text = re.sub(r"--[^\n]*", " ", statement)
            text = re.sub(r"\s+", " ", text).strip()
            for pattern, why in DESTRUCTIVE_PATTERNS:
                if re.search(pattern, text, re.IGNORECASE):
                    problems.append(
                        f"migration {version} statement {index} {why}: {text[:120]!r}. "
                        "Deployed market data must survive every migration; add a new "
                        "column or table instead, and retire the old one in a separate, "
                        "explicitly authorised operation."
                    )
    return problems


def check_append_only(
    migrations: list[tuple[int, list[str]]], base_ref: str, repo_root: pathlib.Path
) -> list[str]:
    """Compare against the migrator on `base_ref`.

    A version present on both revisions must be identical. New versions are
    free. A version that disappears is a failure: deployed databases have
    already recorded it.
    """
    show = subprocess.run(
        ["git", "show", f"{base_ref}:{MIGRATOR_REL}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if show.returncode != 0:
        stderr = show.stderr.strip()
        if "exists on disk, but not in" in stderr or "does not exist" in stderr:
            print(
                f"  note: {MIGRATOR_REL} does not exist on {base_ref}; "
                "treating every migration as new"
            )
            return []
        raise Failure(
            f"cannot read {MIGRATOR_REL} from {base_ref}: {stderr}. "
            "Fetch the base revision before running the append-only check, or pass "
            "--no-append-only if this is a new repository."
        )

    base = dict(_parse_migrations(show.stdout, f"{base_ref}:{MIGRATOR_REL}"))
    current = dict(migrations)

    problems: list[str] = []
    for version, statements in sorted(base.items()):
        if version not in current:
            problems.append(
                f"migration {version} was removed. Databases deployed from {base_ref} "
                "have it recorded in schema_version and will never replay it."
            )
        elif current[version] != statements:
            problems.append(
                f"migration {version} was edited after release. Its statements never "
                f"re-run where version {version} is already recorded, so deployed and "
                "fresh databases would diverge permanently. Append a new version "
                "instead."
            )

    added = sorted(set(current) - set(base))
    if added:
        print(f"  new migrations in this revision: {', '.join(str(v) for v in added)}")
    else:
        print("  no new migrations in this revision")
    return problems


def check_executable(migrations: list[tuple[int, list[str]]]) -> list[str]:
    """Actually run the chain against throwaway SQLite databases."""
    try:
        from sqlalchemy import create_engine, inspect, text
    except ImportError as exc:  # pragma: no cover - environment problem
        raise Failure(
            f"sqlalchemy is needed for the executable checks ({exc}); "
            "install the Database package or pass --static-only"
        ) from exc
    try:
        from market_data_db.metadata.migrator import current_version, run_migrations
        from market_data_db.metadata.models import Base
    except ImportError as exc:  # pragma: no cover - environment problem
        raise Failure(
            f"market_data_db is not importable ({exc}); "
            "install it or pass --static-only"
        ) from exc

    problems: list[str] = []
    head = max(v for v, _ in migrations)
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)

        # 1. A fresh database reaches head.
        fresh = create_engine(f"sqlite:///{root / 'fresh.sqlite'}", future=True)
        run_migrations(fresh)
        reached = current_version(fresh)
        if reached != head:
            problems.append(
                f"a fresh database reached version {reached}, not head ({head})"
            )
        else:
            print(f"  fresh database reached head (version {head})")

        # 2. The schema matches the ORM metadata, so the running server's
        #    queries match what the migration chain actually produced.
        expected = set(Base.metadata.tables)
        actual = set(inspect(fresh).get_table_names())
        missing = expected - actual
        if missing:
            problems.append(
                f"the migrated schema is missing tables the ORM expects: "
                f"{', '.join(sorted(missing))}"
            )
        else:
            print(f"  all {len(expected)} ORM tables present after migration")

        # 3. Re-running is a no-op. run_migrations executes on every engine
        #    construction, so a non-idempotent chain would insert a duplicate
        #    schema_version row -- or re-apply DDL -- on every pod restart.
        run_migrations(fresh)
        with fresh.connect() as conn:
            rows = conn.execute(text("SELECT COUNT(*) FROM schema_version")).scalar()
            distinct = conn.execute(
                text("SELECT COUNT(DISTINCT version) FROM schema_version")
            ).scalar()
        if rows != distinct:
            problems.append(
                f"re-running the migrations duplicated schema_version rows "
                f"({rows} rows for {distinct} versions); the chain is not idempotent "
                "and every pod restart would corrupt the version record"
            )
        elif current_version(fresh) != head:
            problems.append("re-running the migrations moved the version away from head")
        else:
            print("  re-running the chain is a no-op (idempotent)")
        fresh.dispose()

        # 4. Every historical version upgrades to head. This is the check
        #    that matters on a real deployment: the live database is at some
        #    earlier version, not empty.
        for stop_at in [v for v, _ in migrations if v < head]:
            path = root / f"at-{stop_at}.sqlite"
            engine = create_engine(f"sqlite:///{path}", future=True)
            Base.metadata.create_all(engine)
            with engine.begin() as conn:
                for version, statements in migrations:
                    if version > stop_at:
                        break
                    for statement in statements:
                        conn.execute(text(statement))
                    conn.execute(
                        text(
                            "INSERT INTO schema_version (version, applied_at) "
                            "VALUES (:v, datetime('now'))"
                        ),
                        {"v": version},
                    )
            if current_version(engine) != stop_at:
                problems.append(f"could not stage a database at version {stop_at}")
                engine.dispose()
                continue
            run_migrations(engine)
            if current_version(engine) != head:
                problems.append(
                    f"a database at version {stop_at} did not upgrade to head "
                    f"({head}); it stopped at {current_version(engine)}"
                )
            else:
                print(f"  version {stop_at} upgrades to head")
            engine.dispose()

    return problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        default=None,
        help="repository root (default: two levels above this script)",
    )
    parser.add_argument(
        "--base-ref",
        default=None,
        help="git revision to compare against for the append-only check",
    )
    parser.add_argument(
        "--static-only",
        action="store_true",
        help="skip the checks that execute the migration chain",
    )
    parser.add_argument(
        "--no-append-only",
        action="store_true",
        help="skip the append-only comparison (new repository, or no base revision)",
    )
    args = parser.parse_args(argv[1:])

    repo_root = (
        pathlib.Path(args.repo_root).resolve()
        if args.repo_root
        else pathlib.Path(__file__).resolve().parents[2]
    )
    migrator = repo_root / MIGRATOR_REL
    if not migrator.is_file():
        print(f"FATAL: no migrator at {migrator}", file=sys.stderr)
        return 1

    try:
        migrations = _parse_migrations(migrator.read_text(), MIGRATOR_REL)
    except Failure as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1

    print(
        f"Validating {len(migrations)} migration(s) "
        f"(versions {', '.join(str(v) for v, _ in migrations)}) from {MIGRATOR_REL}\n"
    )

    problems: list[str] = []

    print("== Structure ==")
    structure = check_structure(migrations)
    problems += structure
    if not structure:
        print("  versions start at 1, increase strictly and are contiguous")

    print("\n== Destructive statements ==")
    destructive = check_destructive(migrations)
    problems += destructive
    if not destructive:
        print("  no migration drops, truncates, deletes or renames")

    print("\n== Append-only ==")
    if args.no_append_only or not args.base_ref:
        print("  skipped (no base revision given)")
    else:
        try:
            problems += check_append_only(migrations, args.base_ref, repo_root)
        except Failure as exc:
            print(f"FATAL: {exc}", file=sys.stderr)
            return 1

    print("\n== Executable chain ==")
    if args.static_only:
        print("  skipped (--static-only)")
    else:
        try:
            problems += check_executable(migrations)
        except Failure as exc:
            print(f"FATAL: {exc}", file=sys.stderr)
            return 1

    print()
    if problems:
        print(f"FAIL: {len(problems)} migration problem(s):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print("PASS: the migration chain is well formed, non-destructive and replayable.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
