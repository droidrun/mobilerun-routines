"""The README and the CI workflow agree with the code."""

import json
import re
from pathlib import Path

from mobilerun_sandbox import (
    ALLOWED_FORMATS,
    DATASET_MAX_BYTES,
    ERROR_MESSAGE_MAX_CHARS,
    RESERVED_ERROR_CODES,
    RESULT_MAX_BYTES,
    STATE_MAX_BYTES,
)

ROOT = Path(__file__).parent.parent
README = (ROOT / "README.md").read_text()


def _marked(name: str) -> str:
    match = re.search(rf"<!-- {name}:start -->(.*?)<!-- {name}:end -->", README, re.S)
    assert match, f"README has no {name} block"
    return match.group(1)


def test_readme_lists_exactly_the_reserved_codes():
    assert set(re.findall(r"`([a-z_]+)`", _marked("reserved-codes"))) == set(RESERVED_ERROR_CODES)


def test_readme_lists_exactly_the_allowed_formats():
    assert tuple(re.findall(r"`([a-z-]+)`", _marked("formats"))) == ALLOWED_FORMATS


def test_readme_states_the_limits():
    block = _marked("limits")
    for value in (ERROR_MESSAGE_MAX_CHARS, RESULT_MAX_BYTES, STATE_MAX_BYTES, DATASET_MAX_BYTES):
        assert str(value) in block


def test_readme_no_longer_documents_removed_members():
    assert "previous_version" not in README
    assert "complete=" not in README and "`complete`" not in README


def test_readme_documents_the_new_members():
    names = ("previous_schema", "ctx.dataset.push", "key_field", "--dataset-file", "ctx.partial")
    for name in names:
        assert name in README


def test_ci_runs_the_supported_python_versions():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    for version in ("3.11", "3.12", "3.13"):
        assert f'"{version}"' in workflow
    assert "matrix" in workflow


def test_the_readme_sample_is_a_valid_script(tmp_path, capsys):
    from mobilerun_sandbox import cli

    sample = re.search(r"```python\n(.*?)```", README, re.S)
    assert sample
    path = tmp_path / "sample.py"
    path.write_text(sample.group(1))
    assert cli.main(["inspect", str(path)]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["datasetKey"] == "sku" and doc["async"] is True
    assert doc["inputSchema"]["properties"]["since"]["format"] == "date"
    assert set(doc["outputSchema"]["properties"]["items"]["items"]["properties"]) == {
        "sku",
        "name",
        "unitPrice",
    }
