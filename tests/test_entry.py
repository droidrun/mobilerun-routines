import pytest
from pydantic import BaseModel

from mobilerun_sandbox import Context, EntryPointError, script
from mobilerun_sandbox.entry import _ENTRY_POINTS


class In(BaseModel):
    name: str


class Out(BaseModel):
    greeting: str


def _entry():
    (entry,) = _ENTRY_POINTS.values()
    return entry


def test_sync_entry_point():
    @script
    def main(input: In, ctx: Context) -> Out:
        return Out(greeting=input.name)

    entry = _entry()
    assert (entry.name, entry.is_async, entry.takes_ctx) == ("main", False, True)
    assert entry.input_model is In and entry.output_model is Out
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


def test_unannotated_ctx_and_no_return_annotation():
    @script
    def main(input: In, ctx):
        pass

    assert _entry().takes_ctx


def test_rejects_no_parameters():
    with pytest.raises(EntryPointError, match="'input'"):

        @script
        def main():
            pass


def test_rejects_too_many_parameters():
    with pytest.raises(EntryPointError, match="'extra'"):

        @script
        def main(input: In, ctx: Context, extra: int):
            pass


def test_rejects_unannotated_input():
    with pytest.raises(EntryPointError, match="'data'.*annotation"):

        @script
        def main(data):
            pass


def test_rejects_non_model_input():
    with pytest.raises(EntryPointError, match="'input'.*BaseModel"):

        @script
        def main(input: dict) -> None:
            pass


def test_rejects_wrong_ctx_annotation():
    with pytest.raises(EntryPointError, match="'ctx'.*Context"):

        @script
        def main(input: In, ctx: int):
            pass


def test_rejects_non_model_return():
    with pytest.raises(EntryPointError, match="return"):

        @script
        def main(input: In) -> dict:
            return {}


def test_rejects_keyword_only_and_varargs():
    with pytest.raises(EntryPointError, match="'extra'"):

        @script
        def main(input: In, *, extra: int = 1):
            pass


def test_rejects_generator():
    with pytest.raises(EntryPointError, match="generator"):

        @script
        def main(input: In):
            yield 1


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
