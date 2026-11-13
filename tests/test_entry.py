import pytest
from pydantic import BaseModel

from mobilerun_routines import Context, EntryPointError, script
from mobilerun_routines.entry import take


class In(BaseModel):
    name: str


class Out(BaseModel):
    greeting: str


def _entry():
    entry = take(__name__)
    assert entry is not None
    return entry


def test_sync_entry_point():
    @script
    def main(input: In, ctx: Context) -> Out:
        return Out(greeting=input.name)

    entry = _entry()
    assert (entry.name, entry.is_async, entry.takes_ctx) == ("main", False, True)
    assert entry.input_model is In and entry.output_model is Out
    assert entry.output_kind == "value"
    assert main(In(name="a"), None).greeting == "a"  # decorator returns the function unchanged


def test_async_entry_point():
    @script
    async def main(input: In, ctx: Context) -> Out | None:
        return None

    entry = _entry()
    assert entry.is_async and entry.output_model is Out


def test_ctx_is_optional():
    @script
    def main(input: In) -> None:
        pass

    entry = _entry()
    assert not entry.takes_ctx and entry.output_model is None and entry.output_kind == "none"


def test_string_annotation_for_ctx_is_resolved():
    @script
    def main(input: In, ctx: "Context"):
        pass

    assert _entry().takes_ctx


def _no_params():
    pass


def _too_many(input: In, ctx: Context, extra: int):
    pass


def _unannotated_input(data):
    pass


def _non_model_input(input: dict) -> None:
    pass


def _unannotated_ctx(input: In, ctx):
    pass


def _wrong_ctx(input: In, ctx: int):
    pass


def _non_model_return(input: In) -> dict:
    return {}


def _keyword_only(input: In, *, extra: int = 1):
    pass


def _generator(input: In):
    yield 1


@pytest.mark.parametrize(
    ("func", "match"),
    [
        (_no_params, "'input'"),
        (_too_many, "'extra'"),
        (_unannotated_input, "'data'.*annotation"),
        (_non_model_input, "'input'.*BaseModel"),
        (_unannotated_ctx, "'ctx'.*annotation"),
        (_wrong_ctx, "'ctx'.*Context"),
        (_non_model_return, "return"),
        (_keyword_only, "'extra'"),
        (_generator, "generator"),
    ],
)
def test_rejects_wrong_signatures(func, match):
    with pytest.raises(EntryPointError, match=match):
        script(func)


def test_rejects_call_with_arguments():
    with pytest.raises(EntryPointError, match="bare decorator"):
        script()


def test_rejects_duplicate():
    @script
    def first(input: In) -> None:
        pass

    with pytest.raises(EntryPointError, match="duplicate.*first"):

        @script
        def second(input: In) -> None:
            pass
