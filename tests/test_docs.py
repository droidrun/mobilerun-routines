"""The README agrees with the code."""

import json
import re
from pathlib import Path

from mobilerun_sandbox import (
    RESERVED_ERROR_CODES,
)

ROOT = Path(__file__).parent.parent
README = (ROOT / "README.md").read_text()


def _marked(name: str) -> str:
    match = re.search(rf"<!-- {name}:start -->(.*?)<!-- {name}:end -->", README, re.S)
    assert match, f"README has no {name} block"
    return match.group(1)


def test_readme_lists_exactly_the_reserved_codes():
    assert set(re.findall(r"`([a-z_]+)`", _marked("reserved-codes"))) == set(RESERVED_ERROR_CODES)


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
