"""Tests for the scanner-exception gate.

This script is the only chokepoint for suppressing a scanner finding, so a
silent failure here would let an expired or undocumented suppression through
for every scanner at once. As with the other gates, most of these assert
rejection.

The module is exercised through its `validate()` function and through the
command, rather than by rewriting the real exceptions.json, so the
repository's own record is never mutated by a test run.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

HERE = pathlib.Path(__file__).parent
REPO_ROOT = HERE.parents[1]
MODULE_PATH = HERE / "validate-security-exceptions.py"

_spec = importlib.util.spec_from_file_location("validate_security_exceptions", MODULE_PATH)
assert _spec and _spec.loader
vse = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vse)

FAR_FUTURE = "2099-12-31"
LONG_REASON = "a sufficiently detailed reason explaining why this does not apply"


def entry(**overrides):
    base = {
        "scanner": "trivy-image",
        "id": "CVE-2026-00001",
        "reason": LONG_REASON,
        "expires": FAR_FUTURE,
    }
    base.update(overrides)
    # A test may need to drop a key entirely, not just change it.
    return {k: v for k, v in base.items() if v is not None}


# --------------------------------------------------------------------------
# accepted
# --------------------------------------------------------------------------


def test_accepts_a_well_formed_entry():
    live = vse.validate([entry()])
    assert live["trivy-image"] == {"CVE-2026-00001"}


def test_accepts_an_empty_record():
    live = vse.validate([])
    assert all(v == set() for v in live.values())


@pytest.mark.parametrize(
    "scanner,key",
    [
        ("gitleaks", "fingerprint"),
        ("trivy-image", "id"),
        ("trivy-config", "id"),
    ],
)
def test_accepts_every_supported_scanner(scanner, key):
    e = {"scanner": scanner, key: "X-1", "reason": LONG_REASON, "expires": FAR_FUTURE}
    live = vse.validate([e])
    assert live[scanner] == {"X-1"}


def test_groups_entries_by_scanner():
    live = vse.validate(
        [
            entry(scanner="trivy-image", id="CVE-1"),
            entry(scanner="trivy-config", id="KSV-1"),
            {"scanner": "gitleaks", "fingerprint": "f1", "reason": LONG_REASON, "expires": FAR_FUTURE},
        ]
    )
    assert live["trivy-image"] == {"CVE-1"}
    assert live["trivy-config"] == {"KSV-1"}
    assert live["gitleaks"] == {"f1"}


# --------------------------------------------------------------------------
# rejected
# --------------------------------------------------------------------------


def test_rejects_an_expired_entry():
    # The whole point of the expiry: an unfixed CVE is suppressed because
    # there is nothing to upgrade to today, and that stops being true.
    with pytest.raises(SystemExit):
        vse.validate([entry(expires="2020-01-01")])


def test_rejects_a_missing_expiry():
    with pytest.raises(SystemExit):
        vse.validate([entry(expires=None)])


def test_rejects_a_malformed_expiry():
    with pytest.raises(SystemExit):
        vse.validate([entry(expires="31/03/2027")])


def test_rejects_a_missing_reason():
    with pytest.raises(SystemExit):
        vse.validate([entry(reason=None)])


def test_rejects_a_perfunctory_reason():
    # "false positive" with no explanation is indistinguishable from
    # silencing a scanner nobody understood.
    with pytest.raises(SystemExit):
        vse.validate([entry(reason="false positive")])


def test_rejects_an_unknown_scanner():
    with pytest.raises(SystemExit):
        vse.validate([entry(scanner="not-a-scanner")])


def test_rejects_a_missing_identifier():
    with pytest.raises(SystemExit):
        vse.validate([entry(id=None)])


def test_rejects_a_gitleaks_entry_identified_by_id():
    # Gitleaks suppressions are keyed by fingerprint; an `id` would silently
    # produce an empty .gitleaksignore and suppress nothing.
    with pytest.raises(SystemExit):
        vse.validate([{"scanner": "gitleaks", "id": "x", "reason": LONG_REASON, "expires": FAR_FUTURE}])


def test_reports_every_problem_at_once(capsys):
    # One run should surface all of them, not just the first.
    with pytest.raises(SystemExit):
        vse.validate(
            [
                entry(expires="2020-01-01", id="CVE-A"),
                entry(reason="short", id="CVE-B"),
                entry(scanner="bogus", id="CVE-C"),
            ]
        )
    err = capsys.readouterr().err
    assert "3 problem(s)" in err


# --------------------------------------------------------------------------
# emitted ignore files
# --------------------------------------------------------------------------


def test_emit_writes_only_the_requested_scanner(tmp_path):
    live = vse.validate(
        [entry(scanner="trivy-image", id="CVE-1"), entry(scanner="trivy-config", id="KSV-1")]
    )
    out = tmp_path / "ignore"
    vse.emit("trivy-image", out, live)
    body = [l for l in out.read_text().splitlines() if l and not l.startswith("#")]
    assert body == ["CVE-1"]


def test_emit_marks_the_file_as_generated(tmp_path):
    # So nobody edits the ignore file and expects it to survive.
    live = vse.validate([entry(id="CVE-1")])
    out = tmp_path / "ignore"
    vse.emit("trivy-image", out, live)
    assert "do not edit by hand" in out.read_text()


def test_emit_produces_an_empty_list_when_nothing_is_suppressed(tmp_path):
    live = vse.validate([])
    out = tmp_path / "ignore"
    vse.emit("trivy-image", out, live)
    assert [l for l in out.read_text().splitlines() if l and not l.startswith("#")] == []


# --------------------------------------------------------------------------
# the real record, end to end
# --------------------------------------------------------------------------


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(MODULE_PATH), *args], capture_output=True, text=True
    )


def test_the_committed_record_is_valid():
    result = _run()
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_committed_record_parses_and_every_entry_is_complete():
    record = json.loads((REPO_ROOT / "deployment/security/exceptions.json").read_text())
    assert record["schema_version"] == 1
    for e in record["exceptions"]:
        assert e["scanner"] in vse.SCANNERS
        assert e[vse.KEY[e["scanner"]]]
        assert len(e["reason"]) >= 20
        assert e["expires"]


def test_emitting_the_real_record_works_for_every_scanner(tmp_path):
    for scanner in sorted(vse.SCANNERS):
        result = _run("--emit", scanner, str(tmp_path / f"{scanner}.ignore"))
        assert result.returncode == 0, result.stdout + result.stderr
        assert (tmp_path / f"{scanner}.ignore").exists()


def test_emitting_an_unknown_scanner_fails(tmp_path):
    result = _run("--emit", "nope", str(tmp_path / "x"))
    assert result.returncode != 0


def test_gitleaksignore_matches_the_record():
    # The one ignore file that stays committed, so the only one that can
    # drift from the record.
    record = json.loads((REPO_ROOT / "deployment/security/exceptions.json").read_text())
    documented = {
        e["fingerprint"] for e in record["exceptions"] if e["scanner"] == "gitleaks"
    }
    committed = {
        line.strip()
        for line in (REPO_ROOT / ".gitleaksignore").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert committed == documented
