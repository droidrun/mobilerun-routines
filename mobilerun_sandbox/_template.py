"""The starter script `python -m mobilerun_sandbox new` writes."""

STARTER_SCRIPT = '''\
from pydantic import BaseModel
from mobilerun_sandbox import Context, script


class Input(BaseModel):
    name: str
    greeting: str = "Hello"


class Output(BaseModel):
    message: str


@script
def main(input: Input, ctx: Context) -> Output:
    ctx.log.info(f"greeting {input.name}")
    return Output(message=f"{input.greeting}, {input.name}!")
'''
