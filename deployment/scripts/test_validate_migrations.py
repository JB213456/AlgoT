"""Tests for the schema migration gate.

The gate's value is in what it REJECTS, so most of these assert rejection.
A gate that cannot fail is not a gate, and the destructive-statement and
append-only checks are the two that stand between a routine deploy and
unrecoverable market data loss.
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys

import pytest

HERE = pathlib.Path(__file__).parent
MODULE_PATH = HERE / "validate-migrations.py"

# The script's filename is hyphenated (it is run as a command, not imported
# by the application), so it is loaded by path rather than by name.
_spec = importlib.util.spec_from_file_location("validate_migrations", MODULE_PATH)
assert _spec and _spec.loader
vm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vm)


# --------------------------------------------------------------------------
# structure
# --------------------------------------------------------------------------


def test_accepts_a_contiguous_chain():
    problems = vm.check_structure(
        [(1, []), (2, ["ALTER TABLE coverage ADD COLUMN a INT"])]
    )
    assert problems == []


def test_rejects_an_empty_chain():
    assert vm.check_structure([])


def test_rejects_a_chain_not_starting_at_one():
    problems = vm.check_structure([(2, [])])
    assert any("must be version 1" in p for p in problems)


def test_rejects_a_version_gap():
    # A gap makes `current_version() >= target` comparisons lie about what
    # has actually been applied.
    problems = vm.check_structure([(1, []), (3, [])])
    assert any("gap" in p for p in problems)


def test_rejects_a_non_increasing_version():
    problems = vm.check_structure([(1, []), (2, []), (2, [])])
    assert any("increase strictly" in p or "more than once" in p for p in problems)


def test_rejects_a_decreasing_version():
    problems = vm.check_structure([(1, []), (3, []), (2, [])])
    assert any("increase strictly" in p for p in problems)


# --------------------------------------------------------------------------
# destructive statements
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "DROP TABLE coverage",
        "drop table if exists coverage",
        "DROP DATABASE market_data",
        "DROP SCHEMA public CASCADE",
        "ALTER TABLE coverage DROP COLUMN symbol",
        "TRUNCATE ingestion_runs",
        "DELETE FROM coverage WHERE symbol = 'AAPL'",
        "ALTER TABLE coverage RENAME TO coverage_old",
        # Broken across lines: the check normalises whitespace first, so
        # formatting cannot smuggle a destructive statement past it.
        "ALTER TABLE coverage\n    DROP\n    COLUMN symbol",
    ],
)
def test_rejects_destructive_statements(statement):
    assert vm.check_destructive([(2, [statement])]), f"not rejected: {statement!r}"


@pytest.mark.parametrize(
    "statement",
    [
        "ALTER TABLE coverage ADD COLUMN venue VARCHAR(16)",
        "CREATE TABLE corporate_actions (id INTEGER PRIMARY KEY)",
        "CREATE INDEX ix_coverage_symbol ON coverage (symbol)",
        "UPDATE coverage SET venue = 'XLON' WHERE venue IS NULL",
        # "dropped" in a comment is prose, not a statement.
        "ALTER TABLE coverage ADD COLUMN venue VARCHAR(16) -- the old one is dropped later",
    ],
)
def test_accepts_additive_statements(statement):
    assert not vm.check_destructive([(2, [statement])]), f"wrongly rejected: {statement!r}"


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def test_parses_the_real_migrator():
    repo_root = HERE.parents[1]
    source = (repo_root / vm.MIGRATOR_REL).read_text()
    migrations = vm._parse_migrations(source, vm.MIGRATOR_REL)
    assert migrations, "the real migrator declares no migrations"
    assert migrations[0][0] == 1


def test_rejects_a_migrator_without_the_list():
    with pytest.raises(vm.Failure, match="no MIGRATIONS assignment"):
        vm._parse_migrations("X = 1\n", "fake.py")


def test_rejects_a_non_literal_list():
    # A computed MIGRATIONS cannot be audited without executing code from
    # the revision under test, which this gate must never do.
    with pytest.raises(vm.Failure):
        vm._parse_migrations("MIGRATIONS = build_migrations()\n", "fake.py")


def test_rejects_a_malformed_entry():
    with pytest.raises(vm.Failure, match="pair"):
        vm._parse_migrations("MIGRATIONS = [(1,)]\n", "fake.py")


def test_rejects_a_non_string_statement():
    with pytest.raises(vm.Failure, match="list of strings"):
        vm._parse_migrations("MIGRATIONS = [(1, [3])]\n", "fake.py")


# --------------------------------------------------------------------------
# append-only
# --------------------------------------------------------------------------


def _repo_with_migrator(tmp_path: pathlib.Path, body: str) -> pathlib.Path:
    """A throwaway git repo holding one committed revision of the migrator."""
    root = tmp_path / "repo"
    target = root / vm.MIGRATOR_REL
    target.parent.mkdir(parents=True)
    target.write_text(body)
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "PATH": "/usr/bin:/bin",
    }
    subprocess.run(["git", "init", "-q", "-b", "base"], cwd=root, check=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True, env=env)
    return root


def _write(root: pathlib.Path, body: str) -> None:
    (root / vm.MIGRATOR_REL).write_text(body)


BASE = "MIGRATIONS = [(1, []), (2, ['ALTER TABLE coverage ADD COLUMN a INT'])]\n"


def test_append_only_accepts_a_new_migration(tmp_path):
    root = _repo_with_migrator(tmp_path, BASE)
    _write(root, BASE.replace("]\n", ", (3, ['CREATE INDEX ix ON coverage (a)'])]\n"))
    migrations = vm._parse_migrations((root / vm.MIGRATOR_REL).read_text(), "x")
    assert not vm.check_append_only(migrations, "base", root)


def test_append_only_rejects_an_edited_release(tmp_path):
    root = _repo_with_migrator(tmp_path, BASE)
    _write(root, BASE.replace("ADD COLUMN a INT", "ADD COLUMN a BIGINT"))
    migrations = vm._parse_migrations((root / vm.MIGRATOR_REL).read_text(), "x")
    problems = vm.check_append_only(migrations, "base", root)
    assert any("edited after release" in p for p in problems)


def test_append_only_rejects_a_removed_migration(tmp_path):
    root = _repo_with_migrator(tmp_path, BASE)
    _write(root, "MIGRATIONS = [(1, [])]\n")
    migrations = vm._parse_migrations((root / vm.MIGRATOR_REL).read_text(), "x")
    problems = vm.check_append_only(migrations, "base", root)
    assert any("was removed" in p for p in problems)


def test_append_only_tolerates_a_missing_base_file(tmp_path):
    # The first revision that introduces the migrator has no base to
    # compare against; that is not a failure.
    root = tmp_path / "repo"
    (root / "placeholder").parent.mkdir(parents=True, exist_ok=True)
    root.mkdir(exist_ok=True)
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "PATH": "/usr/bin:/bin",
    }
    (root / "README").write_text("x\n")
    subprocess.run(["git", "init", "-q", "-b", "base"], cwd=root, check=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True, env=env)
    assert vm.check_append_only([(1, [])], "base", root) == []


def test_append_only_fails_loudly_on_an_unknown_ref(tmp_path):
    # An unfetched base revision must not be mistaken for "nothing changed".
    root = _repo_with_migrator(tmp_path, BASE)
    with pytest.raises(vm.Failure):
        vm.check_append_only([(1, [])], "no-such-ref", root)


# --------------------------------------------------------------------------
# end to end
# --------------------------------------------------------------------------


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(MODULE_PATH), *args],
        capture_output=True,
        text=True,
    )


def test_the_real_chain_passes_the_static_checks():
    result = _run("--static-only", "--no-append-only")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout


def test_a_destructive_chain_fails_the_command(tmp_path):
    root = tmp_path / "repo"
    target = root / vm.MIGRATOR_REL
    target.parent.mkdir(parents=True)
    target.write_text("MIGRATIONS = [(1, []), (2, ['DROP TABLE coverage'])]\n")
    result = _run("--static-only", "--no-append-only", "--repo-root", str(root))
    assert result.returncode == 1
    assert "drops a table" in result.stderr
