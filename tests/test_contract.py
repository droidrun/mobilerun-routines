"""Contract fixes B1-B3: error codes and messages, state schema, aliases."""

import json

import pytest
from script_source import script_source

from mobilerun_routines import ERROR_MESSAGE_MAX_CHARS, RESERVED_ERROR_CODES

# B1: ScriptError code and message are checked


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


def test_script_error_raised_at_import_is_checked_too(run_script):
    source = script_source("pass") + '\nraise ScriptError("cancelled", "at import")\n'
    r = run_script(source)
    assert r.code == 1 and r.envelope["error"]["code"] == "script_error"


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


@pytest.mark.parametrize(
    ("kind", "key"),
    [("alias", "userName"), ("validation_alias", "user_name"), ("serialization_alias", "userName")],
)
def test_output_instance_with_alias_is_accepted(run_script, inspect_source, kind, key):
    models = _alias_models(ALIAS_KINDS[kind])
    source = script_source(f"return {_alias_construct(kind)}", models=models)
    r = run_script(source)
    assert r.code == 0, r.envelope
    # the result uses the serialization names, which are the names outputSchema advertises
    assert r.envelope["result"] == {key: "a", "plain": 1}
    assert set(r.envelope["result"]) == _output_properties(inspect_source, source)


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



# Fix round 1: re-validation reads field names, never aliases, and ignores computed fields

OVERLAP = """
class In(BaseModel):
    pass

class Out(BaseModel):
    first: int = Field(alias="second")
    second: int = Field(alias="third")
"""

COMPUTED = """
from pydantic import ConfigDict, computed_field

class In(BaseModel):
    pass

class Inner(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: int

    @computed_field
    @property
    def n_plus_one(self) -> int:
        return self.n + 1

class Out(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inner: Inner
    a: int

    @computed_field
    @property
    def double(self) -> int:
        return self.a * 2
"""


def test_overlapping_aliases_keep_their_values(run_script):
    r = run_script(script_source("return Out(second=1, third=2)", models=OVERLAP))
    assert r.code == 0, r.envelope
    assert r.envelope["result"] == {"second": 1, "third": 2}


def test_computed_fields_are_serialised_not_revalidated(run_script, inspect_source):
    source = script_source("return Out(inner=Inner(n=1), a=2)", models=COMPUTED)
    r = run_script(source)
    assert r.code == 0, r.envelope
    assert r.envelope["result"] == {
        "inner": {"n": 1, "n_plus_one": 2},
        "a": 2,
        "double": 4,
    }
    code, out, _ = inspect_source(source)
    assert set(json.loads(out)["outputSchema"]["properties"]) == set(r.envelope["result"])


def test_computed_fields_do_not_hide_a_bad_instance(run_script):
    r = run_script(
        script_source("return Out.model_construct(inner=Inner(n=1), a='x')", models=COMPUTED)
    )
    assert r.code == 1 and r.envelope["error"]["code"] == "output_invalid"


# Fix round 1: an envelope is always written


SNEAKY = """
class Sneaky(str):
    def __eq__(self, other):
        return False
    __hash__ = str.__hash__
"""


def test_script_error_with_a_failing_message_still_ends_in_an_envelope(run_script):
    body = """
    class Bad:
        def __str__(self):
            raise RuntimeError("no")
    raise ScriptError("mine", Bad())
    """
    r = run_script(script_source(body))
    assert r.code == 1
    assert r.envelope["error"] == {
        "code": "script_error",
        "message": "error message could not be formatted",
        "retryable": False,
    }


def test_exception_with_a_failing_str_still_ends_in_an_envelope(run_script):
    body = """
    class Bad(Exception):
        def __str__(self):
            raise RuntimeError("no")
    raise Bad()
    """
    r = run_script(script_source(body))
    assert r.code == 1
    assert r.envelope["error"]["message"] == "error message could not be formatted"


def test_non_string_code_becomes_script_error(run_script):
    r = run_script(script_source('raise ScriptError(5, "mine")'))
    assert r.envelope["error"]["code"] == "script_error"
    assert r.envelope["error"]["message"] == "mine"


def test_a_str_subclass_cannot_smuggle_a_reserved_code(run_script):
    body = SNEAKY + '\nraise ScriptError(Sneaky("cancelled"), "mine")'
    r = run_script(script_source(body))
    assert r.code == 1 and r.envelope["error"]["code"] == "script_error"
