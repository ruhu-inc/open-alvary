"""Independent constants, safe diagnostics and explicit acceptance-mode contracts."""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import get_args
from uuid import UUID

import pytest
from test_canonical import leaf

from ingestion.canonical import PROFILE_HASH, CanonicalValidationError, compile_projection
from rights.acquisition import AcquisitionError
from rights.sql_resources import bundle_hash, render
from schema.assembly import CandidateInput
from schema.canonical import CanonicalPlan
from schema.diagnostics import CANONICAL_REASONS
from schema.policy import PermissionVector
from schema.vocabularies import MATERIALS, PERMISSIONS, MaterialClass

GOLDEN = json.loads(Path("tests/fixtures/oa-text-1-golden.json").read_text())


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=lambda c: c["name"])
def test_python_serializer_matches_frozen_hand_calculated_vectors(case):
    assert PROFILE_HASH == GOLDEN["profile_hash"]
    result = compile_projection(
        CanonicalPlan.model_validate(case["plan"]),
        tuple(CandidateInput.model_validate(c) for c in case["candidates"]),
        tuple(UUID(i) for i in case["order"]),
    )
    assert result.canonical_bytes == case["expected_utf8"].encode()


def test_vocabulary_contracts_and_frozen_migrations_cannot_drift_silently():
    assert tuple(PermissionVector.model_fields) == PERMISSIONS
    assert frozenset(get_args(MaterialClass)) == MATERIALS
    for name in ("0003_policy_foundation.py", "0004_operator_commands.py"):
        tree = ast.parse(Path("migrations/versions", name).read_text())
        constants = {
            n.targets[0].id: ast.literal_eval(n.value)
            for n in tree.body
            if isinstance(n, ast.Assign)
            and isinstance(n.targets[0], ast.Name)
            and n.targets[0].id in {"PERMISSIONS", "MATERIALS"}
        }
        assert constants["PERMISSIONS"] == PERMISSIONS
        if "MATERIALS" in constants:
            assert frozenset(constants["MATERIALS"]) == MATERIALS


def test_all_canonical_rejections_are_static_allowlisted_codes():
    tree = ast.parse(Path("ingestion/canonical.py").read_text())
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "fail"
    ]
    assert len(calls) == 28
    assert all(
        len(c.args) == 1 and isinstance(c.args[0], ast.Constant) and c.args[0].value in CANONICAL_REASONS
        for c in calls
    )
    c = leaf(1, "private-marker-do-not-echo")
    with pytest.raises(CanonicalValidationError) as failure:
        compile_projection(CanonicalPlan.model_validate({"blocks": []}), (c,), (c.id,))
    assert failure.value.reason_code == "candidate_coverage_or_order"
    assert c.text not in str(failure.value) and str(c.id) not in str(failure.value)
    assert AcquisitionError("validation_failed", "private-marker-do-not-echo").reason_code is None


def test_every_sql_revision_is_pinned_and_unknown_tokens_fail_closed():
    for revision in ("0011", "0012"):
        tree = ast.parse(next(Path("migrations/versions").glob(revision + "*.py")).read_text())
        pin = next(
            ast.literal_eval(n.value)
            for n in tree.body
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "RESOURCE_HASH" for t in n.targets)
        )
        assert bundle_hash("v" + revision) == pin
    with pytest.raises(ValueError, match="Unknown SQL resource token"):
        render("SELECT {{unapproved}}", {})


def test_default_test_command_requires_postgresql_instead_of_reporting_partial_success():
    env = dict(os.environ)
    env.pop("TEST_DATABASE_URL", None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_postgres.py"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert "Full acceptance requires TEST_DATABASE_URL" in result.stderr
    assert "passed" not in result.stdout
