"""Static checks of a script file, and the repair hints `inspect` reports.

The checks read the script as a Python syntax tree and never execute it, so they can report every
problem at once, with line numbers, before the script is imported.
"""

from __future__ import annotations

import ast
import warnings
from dataclasses import dataclass
from typing import Any

PACKAGE = "mobilerun_routines"

# One hint for every "the script is not set up as a routine script" problem.
SCRIPT_HINT = (
    "Add `from mobilerun_routines import Context, script` and decorate one function: "
    "`@script` `def main(input: Input, ctx: Context) -> Output:`"
)

# Modules newcomers import `script` from by mistake.
_WRONG_MODULES = frozenset({"mobilerun", "mobilerun_sandbox", "mobilerun_sdk"})
_SDK_NAMES = ("script", "Context")


@dataclass(frozen=True)
class Issue:
    """One problem (or warning) found in a script."""

    code: str
    message: str
    line: int | None = None
    hint: str | None = None

    def as_json(self) -> dict[str, Any]:
        document: dict[str, Any] = {"code": self.code, "message": " ".join(self.message.split())}
        if self.line is not None:
            document["line"] = self.line
        if self.hint is not None:
            document["repair"] = {"action": "fix_script", "hint": self.hint}
        return document

    def describe(self) -> str:
        """One line for stderr: `line N: message — hint`."""
        text = " ".join(self.message.split())
        if self.hint is not None:
            text += f" — {self.hint}"
        return f"line {self.line}: {text}" if self.line is not None else text


class _Scan:
    """What the script imports and where it marks entry points."""

    def __init__(self, tree: ast.Module) -> None:
        self.tree = tree
        self.issues: list[Issue] = []
        self.package_script: set[str] = set()  # local names of `mobilerun_routines.script`
        self.package_modules: set[str] = set()  # local names of the `mobilerun_routines` module
        self.mistaken_script: set[str] = set()  # `script` imported from the wrong module
        self.own_script = False  # the script binds the name `script` itself
        self.entry_lines: list[tuple[int, ast.expr]] = []
        self._seen: set[int] = set()
        self._missing_reported = False

    def run(self) -> list[Issue]:
        nodes = list(ast.walk(self.tree))
        for node in nodes:
            if isinstance(node, ast.ImportFrom):
                self._from_import(node)
            elif isinstance(node, ast.Import):
                self._import(node)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.own_script |= node.name == "script"
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                self.own_script |= node.id == "script"
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    call = isinstance(decorator, ast.Call)
                    self._candidate(decorator.func if call else decorator)
            elif isinstance(node, ast.Call):
                self._candidate(node.func)
        self._duplicates()
        if not self.entry_lines:
            self.issues.append(
                Issue("no_entry_point", "no entry point: mark one function with @script",
                      hint=SCRIPT_HINT)
            )
        self.issues.sort(key=lambda issue: (issue.line is None, issue.line or 0))
        return self.issues

    def _from_import(self, node: ast.ImportFrom) -> None:
        if node.level:  # a sibling file of the script; nothing to say about it
            return
        module = node.module or ""
        root = module.split(".")[0]
        for alias in node.names:
            local = alias.asname or alias.name
            if root == PACKAGE:
                if alias.name in ("script", "*"):
                    self.package_script.add(local if alias.name == "script" else "script")
                continue
            mistaken = alias.name == "script" or (
                alias.name == "Context" and root.startswith("mobilerun")
            )
            if alias.name == "script":
                self.mistaken_script.add(local)
            if mistaken:
                self.issues.append(
                    Issue(
                        "import_wrong_module",
                        f"`{alias.name}` is not in `{module}`; it comes from `{PACKAGE}`",
                        node.lineno,
                        SCRIPT_HINT,
                    )
                )
            elif local == "script":
                self.own_script = True

    def _import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name.split(".")[0] == PACKAGE:
                self.package_modules.add(alias.asname or PACKAGE)

    def _candidate(self, target: ast.expr) -> None:
        """Count `target` as an entry point marker if it names the `script` decorator."""
        if id(target) in self._seen:
            return
        self._seen.add(id(target))
        if isinstance(target, ast.Name):
            if target.id not in self.package_script and target.id not in self.mistaken_script:
                if target.id != "script":
                    return
                if not self.own_script and not self._missing_reported:
                    self._missing_reported = True
                    self.issues.append(
                        Issue(
                            "import_missing",
                            "`script` is used but never imported",
                            target.lineno,
                            SCRIPT_HINT,
                        )
                    )
        elif not (isinstance(target, ast.Attribute) and target.attr == "script"):
            # `other.script` also counts as a marker; the import at run time decides if it works.
            return
        self.entry_lines.append((target.lineno, target))

    def _duplicates(self) -> None:
        """Two module-level functions marked with `@script`: the loader rejects the second."""
        marked = {id(target) for _, target in self.entry_lines}
        first: str | None = None
        for node in self.tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            lines = [
                d.lineno
                for d in node.decorator_list
                if id(d.func if isinstance(d, ast.Call) else d) in marked
            ]
            if not lines:
                continue
            if first is None:
                first = node.name
                continue
            self.issues.append(
                Issue(
                    "entry_point_invalid",
                    f"duplicate entry point '{node.name}': '{first}' is already marked with "
                    "@script (one per module)",
                    lines[0],
                    "Keep `@script` on exactly one function.",
                )
            )


def check_source(source: bytes | str, filename: str) -> list[Issue]:
    """Every static problem in `source`; empty when it passes. Never runs the code."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(source, filename)
            # The syntax tree alone misses errors such as `return` outside a function.
            compile(tree, filename, "exec", dont_inherit=True)
    except SyntaxError as exc:
        return [_syntax_issue(exc.msg, exc.lineno)]
    except (ValueError, RecursionError, MemoryError) as exc:
        return [_syntax_issue(str(exc) or type(exc).__name__, None)]
    return _Scan(tree).run()


def _syntax_issue(message: str, line: int | None) -> Issue:
    hint = f"Fix the syntax error on line {line}." if line is not None else None
    return Issue("syntax_error", f"syntax error: {message}", line, hint)


def import_issue(exc: BaseException, path: str) -> Issue:
    """The `import_failed` issue for `exc`, raised while the script at `path` was imported."""
    return _runtime_issue("import_failed", f"{type(exc).__name__}: {exc}", exc, path)


def entry_point_issue(exc: BaseException, path: str) -> Issue:
    """The `entry_point_invalid` issue for an `EntryPointError` the script's decorator raised."""
    return _runtime_issue("entry_point_invalid", str(exc), exc, path)


def _runtime_issue(code: str, message: str, exc: BaseException, path: str) -> Issue:
    line = _script_line(exc, path)
    if line is None and isinstance(exc, SyntaxError):
        line = exc.lineno
    hint = SCRIPT_HINT if _is_setup_mistake(exc) else None
    return Issue(code, message, line, hint)


def _is_setup_mistake(exc: BaseException | None) -> bool:
    """A missing `script`/`Context` name or an import of the wrong SDK module, at the exception
    or its cause (the loader reports unresolved annotations with the `NameError` as cause)."""
    while exc is not None:
        if isinstance(exc, NameError) and exc.name in _SDK_NAMES:
            return True
        if isinstance(exc, ModuleNotFoundError):
            if (exc.name or "").split(".")[0] in _WRONG_MODULES:
                return True
        exc = exc.__cause__
    return False


def _script_line(exc: BaseException, path: str) -> int | None:
    """Line of the innermost traceback frame that belongs to the script file."""
    line = None
    tb = exc.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code.co_filename == path:
            line = tb.tb_lineno
        tb = tb.tb_next
    return line
