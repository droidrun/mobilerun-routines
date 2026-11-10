"""Command line: `python -m mobilerun_sandbox inspect|run`."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import NoReturn, get_args

from ._version import SDK_VERSION
from .context import RunInfo, RunMode
from .errors import EntryPointError
from .loader import ScriptImportError, load_script
from .runner import (
    EXIT_NO_ENTRY_POINT,
    EXIT_OK,
    EXIT_RUNNER_FAULT,
    RunnerFault,
    RunOptions,
    run,
)
from .schema import SchemaError, input_schema, output_schema

MODES = get_args(RunMode)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse exits 2 by default, which `run` uses for invalid input.
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: error: {message}\n")
        raise SystemExit(EXIT_RUNNER_FAULT)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -m mobilerun_sandbox")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    inspect_p = sub.add_parser("inspect", help="print entry point and JSON Schemas")
    inspect_p.add_argument("path")

    run_p = sub.add_parser("run", help="validate input, call the entry point, write the result")
    run_p.add_argument("path")
    run_p.add_argument("--input-file", required=True)
    run_p.add_argument("--result-file", required=True)
    run_p.add_argument("--state-file")
    run_p.add_argument("--run-id", default="local")
    run_p.add_argument("--attempt", type=int, default=1)
    run_p.add_argument("--mode", choices=MODES, default="live")
    run_p.add_argument("--device-id")
    run_p.add_argument("--deadline", type=datetime.fromisoformat)
    return parser


def _inspect(path: str) -> int:
    try:
        entry = load_script(path)
        document = {
            "entrypoint": entry.name,
            "async": entry.is_async,
            "inputSchema": input_schema(entry.input_model),
            "outputSchema": output_schema(entry.output_model) if entry.output_model else None,
            "outputKind": entry.output_kind,
            "sdkVersion": SDK_VERSION,
        }
    except OSError as exc:
        return _reject(f"cannot read script: {exc}")
    except (EntryPointError, ScriptImportError, SchemaError) as exc:
        return _reject(str(exc))
    sys.stdout.write(json.dumps(document) + "\n")
    return EXIT_OK


def _reject(message: str) -> int:
    sys.stderr.write(" ".join(message.split()) + "\n")
    return EXIT_NO_ENTRY_POINT


def _run(args: argparse.Namespace) -> int:
    info = RunInfo(
        run_id=args.run_id,
        attempt=args.attempt,
        deadline=args.deadline,
        mode=args.mode,
        device_id=args.device_id,
    )
    opts = RunOptions(
        path=args.path,
        input_file=args.input_file,
        result_file=args.result_file,
        run_info=info,
        state_file=args.state_file,
    )
    try:
        return run(opts)
    except EntryPointError as exc:
        sys.stderr.write(f"{exc}\n")
        return EXIT_NO_ENTRY_POINT
    except RunnerFault as exc:
        sys.stderr.write(f"{exc}\n")
        return EXIT_RUNNER_FAULT


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "inspect":
        return _inspect(args.path)
    return _run(args)
