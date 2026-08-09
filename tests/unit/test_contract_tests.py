from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from uuid import UUID

import pytest

from marvin_pilot.contract_tests import (
    ContractAccountConfig,
    account_config_schema_json,
    generate_contract_suite,
    load_account_config,
    verify_contract_suite,
    write_contract_suite,
)
from marvin_pilot.errors import PlanSemanticError, PlanSyntaxError

RUN_ID = UUID("22222222-2222-4222-8222-222222222222")
CREATED_AT = "2026-08-08T20:00:00-07:00"
BASE_DATE = date(2026, 8, 10)


def complete_config() -> ContractAccountConfig:
    return ContractAccountConfig.model_validate(
        {
            "sampleOnly": True,
            "fixturePrefix": "DO NOT APPLY - Marvin Pilot bundled sample",
            "parent": {"id": "sample-only-parent-id", "title": "Sample parent"},
            "label": {"id": "sample-only-label-id", "title": "Sample label"},
            "customSectionIds": ["sample-custom-section-a", "sample-custom-section-b"],
            "timeBlockSectionIds": [
                "sample-time-block-section-a",
                "sample-time-block-section-b",
            ],
            "nonTaskDocument": {
                "id": "sample-only-non-task-id",
                "title": "Sample non-task document",
            },
        }
    )


def sample_suite(*, include_limit_cases: bool = False):
    return generate_contract_suite(
        complete_config(),
        run_id=RUN_ID,
        created_at=CREATED_AT,
        base_date=BASE_DATE,
        scale_count=10,
        include_limit_cases=include_limit_cases,
    )


def test_account_config_schema_is_closed_and_secret_free(tmp_path: Path) -> None:
    schema = json.loads(account_config_schema_json())
    assert schema["additionalProperties"] is False
    rendered = json.dumps(schema).lower()
    assert "token" not in rendered
    assert "credential" not in rendered

    path = tmp_path / "account.json"
    path.write_text('{"fullAccessToken":"must-not-be-accepted"}', encoding="utf-8")
    with pytest.raises(PlanSyntaxError, match="invalid contract account config"):
        load_account_config(path)

    example = Path(__file__).resolve().parents[2] / "contract-tests" / "account.example.json"
    assert load_account_config(example).sampleOnly is True


def test_live_account_config_rejects_unreplaced_placeholders() -> None:
    with pytest.raises(ValueError, match="replace or remove"):
        ContractAccountConfig.model_validate(
            {
                "sampleOnly": False,
                "parent": {"id": "replace-with-real-parent-id", "title": "Placeholder"},
            }
        )


def test_generator_is_deterministic_valid_and_capability_scoped() -> None:
    first = sample_suite()
    second = sample_suite()
    assert first == second
    assert len(first.cases) == 25
    assert all(first.manifest["coverage"].values())
    assert first.manifest["sampleOnly"] is True

    for case in first.cases:
        raw = (json.dumps(case.plan, indent=2) + "\n").encode()
        assert b"fullAccess" not in raw
        assert b"/doc/delete" not in raw
        if case.expected_offline == "valid":
            from marvin_pilot.plan_io import parse_plan_bytes

            parse_plan_bytes(raw)


def test_suite_write_verify_hash_and_no_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "suite"
    suite = sample_suite()
    write_contract_suite(output, suite)
    assert verify_contract_suite(output) == (25, 0)

    with pytest.raises(PlanSyntaxError, match="refusing to overwrite"):
        write_contract_suite(output, suite)

    first_case = output / suite.cases[0].filename
    first_case.write_text(first_case.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(PlanSyntaxError, match="hash mismatch"):
        verify_contract_suite(output)


def test_limit_cases_encode_500_valid_and_501_invalid(tmp_path: Path) -> None:
    output = tmp_path / "suite"
    suite = sample_suite(include_limit_cases=True)
    write_contract_suite(output, suite)
    assert verify_contract_suite(output) == (26, 1)
    boundary = {
        case.case_id: (len(case.plan["operations"]), case.expected_offline)
        for case in suite.cases
        if case.case_id.startswith("offline-limit")
    }
    assert boundary == {
        "offline-limit-500": (500, "valid"),
        "offline-limit-501": (501, "invalid"),
    }


@pytest.mark.parametrize("scale_count", [0, 501])
def test_generator_rejects_out_of_range_scale(scale_count: int) -> None:
    with pytest.raises(PlanSemanticError, match="between 1 and 500"):
        generate_contract_suite(
            ContractAccountConfig(),
            run_id=RUN_ID,
            created_at=CREATED_AT,
            base_date=BASE_DATE,
            scale_count=scale_count,
        )


def test_committed_sample_snapshot_matches_generator() -> None:
    root = Path(__file__).resolve().parents[2]
    sample_path = root / "contract-tests" / "samples"
    assert verify_contract_suite(sample_path) == (25, 0)
    expected = sample_suite()
    actual_manifest = json.loads((sample_path / "manifest.json").read_text(encoding="utf-8"))
    assert actual_manifest == expected.manifest
    for case in expected.cases:
        actual = json.loads((sample_path / case.filename).read_text(encoding="utf-8"))
        assert actual == case.plan
