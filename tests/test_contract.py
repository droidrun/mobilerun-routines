"""Contract fixes B1-B3: error codes and messages, state schema, aliases."""

import json

import pytest
from script_source import script_source

from mobilerun_sandbox import (
    ERROR_MESSAGE_MAX_CHARS,
    RESERVED_ERROR_CODES,
)

# B1: ScriptError code and message are checked


def test_reserved_codes_are_the_documented_set():
    assert set(RESERVED_ERROR_CODES) == {
        "script_error",
        "input_invalid",
        "output_invalid",
        "result_too_large",
        "state_too_large",
        "dataset_too_large",
        "cancelled",
    }


@pytest.mark.parametrize("code", sorted(RESERVED_ERROR_CODES))
def test_script_cannot_take_a_reserved_code(run_script, code):
    r = run_script(script_source(f'raise ScriptError({code!r}, "mine", retryable=True)'))
    assert r.code == 1
    assert r.envelope == {
        "ok": False,
        "error": {"code": "script_error", "message": "mine", "retryable": True},
    }


@pytest.mark.parametrize(
    "code",
    ["", "Bad", "1abc", "_x", "a-b", "a b", "x" * 65, "é"],
    ids=["empty", "upper", "digit-first", "underscore", "dash", "space", "65-chars", "accent"],
)
def test_script_error_code_must_match_the_pattern(run_script, code):
    r = run_script(script_source(f'raise ScriptError({code!r}, "mine")'))
    assert r.code == 1 and r.envelope["error"]["code"] == "script_error"
    assert r.envelope["error"]["message"] == "mine"


@pytest.mark.parametrize("code", ["a", "login_failed", "a1_b2", "x" * 64])
def test_valid_script_error_codes_are_kept(run_script, code):
    r = run_script(script_source(f'raise ScriptError({code!r}, "mine")'))
    assert r.envelope["error"]["code"] == code


def test_script_error_message_is_cut(run_script):
    r = run_script(script_source('raise ScriptError("too_big", "x" * 2_000_000)'))
    assert r.code == 1
    assert r.envelope["error"]["code"] == "too_big"
    assert r.envelope["error"]["message"] == "x" * ERROR_MESSAGE_MAX_CHARS
    assert len(json.dumps(r.envelope)) < 1000


def test_reserved_code_with_huge_message_is_small_and_script_error(run_script):
    r = run_script(script_source('raise ScriptError("input_invalid", "x" * 2_000_000)'))
    assert r.code == 1
    assert r.envelope["error"]["code"] == "script_error"
    assert len(r.envelope["error"]["message"]) == ERROR_MESSAGE_MAX_CHARS


def test_script_error_raised_at_import_is_checked_too(run_script):
    source = script_source("pass") + '\nraise ScriptError("cancelled", "at import")\n'
    r = run_script(source)
    assert r.code == 1 and r.envelope["error"]["code"] == "script_error"


def test_script_error_constructor_does_not_check(run_script):
    from mobilerun_sandbox import ScriptError

    err = ScriptError("input_invalid", "m")  # the runner checks when it builds the envelope
    assert err.code == "input_invalid"


# B2: the schema of the previous state reaches the script


def _schema_script(extra: str = ""):
    return script_source(
        f'ctx.log.info(f"schema={{ctx.state.previous_schema}}")\n{extra}\nreturn Out(greeting="ok")'
    )


@pytest.mark.parametrize("schema", [1, 2, 9])
def test_previous_schema_is_exposed(run_script, schema):
    state = {"version": 7, "meta": {"schema": schema}, "data": {"n": 1}}
    r = run_script(_schema_script(), state=state)
    assert r.code == 0 and f"schema={schema}" in r.err


def test_previous_schema_is_none_without_a_previous_state(run_script):
    for kwargs in ({}, {"state": None}):
        r = run_script(_schema_script(), **kwargs)
        assert r.code == 0 and "schema=None" in r.err


def test_previous_version_is_not_public(run_script):
    body = 'return Out(greeting=str(hasattr(ctx.state, "previous_version")))'
    r = run_script(script_source(body), state={"version": 7, "meta": {"schema": 1}, "data": 1})
    assert r.envelope["result"] == {"greeting": "False"}


def test_state_file_version_is_parsed_and_ignored(run_script):
    r = run_script(_schema_script(), state={"version": 123456, "meta": {"schema": 4}, "data": 0})
    assert r.code == 0 and "schema=4" in r.err


# B3: aliases

ALIAS_KINDS = {
    "alias": 'user_name: str = Field(alias="userName")',
    "validation_alias": 'user_name: str = Field(validation_alias="userName")',
    "serialization_alias": 'user_name: str = Field(serialization_alias="userName")',
}


def _alias_models(field: str) -> str:
    return f"""
    class In(BaseModel):
        pass

    class Out(BaseModel):
        {field}
        plain: int = 1
    """


def _alias_construct(kind: str) -> str:
    # `alias` and `validation_alias` are filled through their alias, serialization_alias by name
    return 'Out(userName="a")' if kind in ("alias", "validation_alias") else 'Out(user_name="a")'


def _output_properties(inspect_source, source: str) -> set[str]:
    code, out, _ = inspect_source(source)
    assert code == 0
    return set(json.loads(out)["outputSchema"]["properties"])


@pytest.mark.parametrize("kind", ALIAS_KINDS)
def test_output_instance_with_alias_is_accepted(run_script, inspect_source, kind):
    models = _alias_models(ALIAS_KINDS[kind])
    source = script_source(f"return {_alias_construct(kind)}", models=models)
    r = run_script(source)
    assert r.code == 0, r.envelope
    # the result uses the names outputSchema advertises
    assert set(r.envelope["result"]) == _output_properties(inspect_source, source)


@pytest.mark.parametrize(
    ("kind", "key"),
    [("alias", "userName"), ("validation_alias", "user_name"), ("serialization_alias", "userName")],
)
def test_result_key_is_the_serialization_name(run_script, kind, key):
    models = _alias_models(ALIAS_KINDS[kind])
    source = script_source(f"return {_alias_construct(kind)}", models=models)
    r = run_script(source)
    assert r.envelope["result"] == {key: "a", "plain": 1}


@pytest.mark.parametrize("kind", ALIAS_KINDS)
def test_aliased_instance_built_without_validation_is_still_checked(run_script, kind):
    # model_construct skips validation, so the value must be re-validated
    models = _alias_models(ALIAS_KINDS[kind])
    ok = run_script(script_source("return Out.model_construct(user_name='a')", models=models))
    assert ok.code == 0, ok.envelope
    bad = run_script(script_source("return Out.model_construct(user_name=5)", models=models))
    assert bad.code == 1 and bad.envelope["error"]["code"] == "output_invalid"


@pytest.mark.parametrize("kind", ALIAS_KINDS)
def test_aliased_instance_mutated_after_construction_is_checked(run_script, kind):
    body = f"out = {_alias_construct(kind)}\nout.user_name = 5\nreturn out"
    r = run_script(script_source(body, models=_alias_models(ALIAS_KINDS[kind])))
    assert r.code == 1 and r.envelope["error"]["code"] == "output_invalid"


NESTED_ALIAS = """
class In(BaseModel):
    pass

class Inner(BaseModel):
    in_v: int = Field(serialization_alias="inV")

class Out(BaseModel):
    inner: Inner
    many: list[Inner]
    out_v: str = Field(validation_alias="outIn", serialization_alias="outV")
"""


def test_nested_aliases_round_trip(run_script, inspect_source):
    body = 'return Out(inner=Inner(in_v=1), many=[Inner(in_v=2)], outIn="x")'
    source = script_source(body, models=NESTED_ALIAS)
    r = run_script(source)
    assert r.code == 0, r.envelope
    assert r.envelope["result"] == {"inner": {"inV": 1}, "many": [{"inV": 2}], "outV": "x"}
    code, out, _ = inspect_source(source)
    schema = json.loads(out)["outputSchema"]
    assert set(schema["properties"]) == set(r.envelope["result"])
    assert set(schema["properties"]["inner"]["properties"]) == {"inV"}


def test_nested_alias_error_details_use_declared_names(run_script):
    body = "return Out.model_construct(inner=Inner.model_construct(in_v='x'), many=[], out_v='s')"
    r = run_script(script_source(body, models=NESTED_ALIAS))
    assert r.code == 1 and r.envelope["error"]["code"] == "output_invalid"
    assert "x" not in json.dumps(r.envelope["error"]["details"])


def test_dict_return_uses_validation_names(run_script):
    models = _alias_models(ALIAS_KINDS["alias"])
    r = run_script(script_source('return {"userName": "d"}', models=models))
    assert r.code == 0 and r.envelope["result"] == {"userName": "d", "plain": 1}

