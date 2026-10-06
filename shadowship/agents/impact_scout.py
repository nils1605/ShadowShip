"""Impact Scout agent: map the blast radius of a diff.

  1. List changed files and the top-level functions / classes / assignments the diff touches.
  2. Find dependents: files that import a changed module.
  3. Follow references between functions (by name) until reaching API route handlers
     (functions decorated with @app.get/post/...). Those routes are the affected routes.
  4. Summarize the intent from commit messages, and add rule-based risk hints.

Matching is by name, which is simple and good enough for a small service; it can over-report
when two functions share a name, never under-report within the scanned files.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

from shadowship.gitutil import GitError, git
from shadowship.models import ImpactReport, Journey

HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
MODULE_LEVEL = "(module)"
MONEY_WORDS = ("price", "pricing", "tax", "discount", "total", "payment", "money", "refund", "currency")

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


# --- Parsing the diff ------------------------------------------------------------------


def changed_line_numbers(diff_text: str) -> dict[str, tuple[set[int], set[int]]]:
    """Parse `git diff -U0` output into {path: (removed line numbers in old file, added lines in new file)}."""
    changes: dict[str, tuple[set[int], set[int]]] = {}
    old_path = new_path = None
    for line in diff_text.splitlines():
        if line.startswith("--- "):
            old_path = None if line == "--- /dev/null" else line[6:]
        elif line.startswith("+++ "):
            new_path = None if line == "+++ /dev/null" else line[6:]
        elif match := _HUNK.match(line):
            path = new_path or old_path
            old_start, old_count = int(match.group(1)), int(match.group(2) or 1)
            new_start, new_count = int(match.group(3)), int(match.group(4) or 1)
            removed, added = changes.setdefault(path, (set(), set()))
            removed.update(range(old_start, old_start + old_count))
            added.update(range(new_start, new_start + new_count))
    return changes


# --- Reading Python source -------------------------------------------------------------


@dataclass
class Symbol:
    """A top-level function, class, or assignment in a file."""

    file: str
    name: str
    start: int
    end: int
    references: set[str] = field(default_factory=set)
    route: str | None = None  # e.g. "POST /checkout" if this is a route handler
    is_function: bool = False


def _referenced_names(node: ast.AST) -> set[str]:
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
    return names


def _route_of(node: ast.AST) -> str | None:
    for decorator in getattr(node, "decorator_list", []):
        if (
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr in HTTP_METHODS
            and decorator.args
            and isinstance(decorator.args[0], ast.Constant)
            and isinstance(decorator.args[0].value, str)
        ):
            return f"{decorator.func.attr.upper()} {decorator.args[0].value}"
    return None


def top_level_symbols(path: str, source: str) -> list[Symbol]:
    """Top-level functions, classes, and assigned names in a Python file."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    symbols = []
    for node in tree.body:
        is_function = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        if is_function:
            names = [node.name]
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
        else:
            continue
        # A function's lineno is its `def` line; include its decorators.
        start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
        for name in names:
            symbols.append(Symbol(
                path, name, start, node.end_lineno or node.lineno,
                _referenced_names(node), _route_of(node), is_function,
            ))
    return symbols


def imported_modules(source: str) -> set[str]:
    """Dotted module names a file imports, including `from pkg import module` forms."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def module_name(path: str) -> str:
    """'demo_app/pricing.py' -> 'demo_app.pricing'."""
    name = path[:-3].replace("/", ".")
    return name[: -len(".__init__")] if name.endswith(".__init__") else name


def is_test_file(path: str) -> bool:
    name = Path(path).name
    return "tests/" in path or name.startswith("test_") or name.endswith("_test.py")


def _symbols_on_lines(symbols: list[Symbol], source: str, lines: set[int]) -> set[str]:
    """Names of the symbols containing `lines`; MODULE_LEVEL for real code outside any symbol."""
    text = source.splitlines()
    found = set()
    for line in lines:
        owner = next((s.name for s in symbols if s.start <= line <= s.end), None)
        if owner:
            found.add(owner)
        elif line <= len(text) and text[line - 1].strip() and not text[line - 1].strip().startswith("#"):
            found.add(MODULE_LEVEL)
    return found


# --- Routes and journeys -------------------------------------------------------------------


def route_matches(route: str, method: str, path: str) -> bool:
    """Does "POST /cart/{cart_id}/items" match a concrete request like POST /cart/abc/items?"""
    route_method, _, template = route.partition(" ")
    pattern = "^" + re.sub(r"\\\{[^/]+?\\\}", "[^/]+", re.escape(template)) + "$"
    return route_method == method.upper() and re.match(pattern, path.split("?")[0]) is not None


def journeys_hitting(routes: list[str], journeys: list[Journey]) -> list[str]:
    """Names of journeys with at least one step whose path matches an affected route."""
    def step_path(path: str) -> str:
        return re.sub(r"\{\w+\}", "x", path)  # placeholders stand in for any id

    return [
        j.name
        for j in journeys
        if any(route_matches(route, step.method, step_path(step.path)) for step in j.steps for route in routes)
    ]


# --- Intent and risk hints ----------------------------------------------------------------


def commit_messages(repo_root: str | Path, base: str, head: str) -> list[str]:
    log = git(repo_root, "log", "--reverse", "--format=%B%x1e", f"{base}..{head}")
    return [message.strip() for message in log.split("\x1e") if message.strip()]


def intent_from_commits(messages: list[str]) -> str:
    """Fallback intent summary: the commit messages verbatim (trailers dropped)."""
    cleaned = []
    for message in messages:
        lines = [line.strip() for line in message.splitlines() if not re.match(r"^[\w-]+: .+@", line)]
        sentences = [line if line[-1] in ".!?" else line + "." for line in lines if line]
        cleaned.append(" ".join(sentences))
    return " ".join(cleaned) or "No commit messages found."


def risk_hints(
    changed: dict[str, set[str]],
    affected_routes: list[str],
    changed_test_sources: dict[str, str],
    removed: list[str],
    functions: set[str],
) -> list[str]:
    hints = []
    code_symbols = {
        name for path, names in changed.items() if not is_test_file(path) for name in names if name != MODULE_LEVEL
    }

    money = sorted(n for n in code_symbols if any(word in n.lower() for word in MONEY_WORDS))
    if money or any(word in path.lower() for path in changed for word in MONEY_WORDS):
        hints.append(
            "Money-related code changed" + (f" ({', '.join(money)})" if money else "")
            + ": totals may shift for existing customers."
        )

    test_text = "\n".join(changed_test_sources.values())
    untested = sorted(
        n for n in code_symbols & functions if not re.search(rf"\b{re.escape(n)}\b", test_text)
    )
    untested_routes = [r for r in affected_routes if r.split(" ", 1)[1] not in test_text]
    if untested:
        hints.append(f"Changed without any updated test referencing it: {', '.join(untested)}.")
    if untested_routes and changed_test_sources:
        hints.append(f"Updated tests do not call the affected route(s): {', '.join(untested_routes)}.")
    elif not changed_test_sources and code_symbols:
        hints.append("No tests were added or changed.")

    for symbol in removed:
        hints.append(f"Removed or renamed: {symbol}. Callers elsewhere may break.")
    return hints


# --- Orchestration ------------------------------------------------------------------------


def _read(repo_root: str | Path, ref: str, path: str) -> str:
    try:
        return git(repo_root, "show", f"{ref}:{path}")
    except GitError:
        return ""


def scout(base: str, head: str, repo_root: str | Path = ".", journeys: list[Journey] | None = None) -> ImpactReport:
    """Map the blast radius of the changes in `head` since it branched from `base`."""
    merge_base = git(repo_root, "merge-base", base, head)
    changed_files = git(repo_root, "diff", "--name-only", f"{merge_base}..{head}").splitlines()
    line_changes = changed_line_numbers(git(repo_root, "diff", "-U0", f"{merge_base}..{head}", "--", "*.py"))

    # Which top-level symbols did the diff touch, on either side?
    changed: dict[str, set[str]] = {}
    removed: list[str] = []
    functions: set[str] = set()
    for path, (old_lines, new_lines) in line_changes.items():
        old_source, new_source = _read(repo_root, merge_base, path), _read(repo_root, head, path)
        old_symbols, new_symbols = top_level_symbols(path, old_source), top_level_symbols(path, new_source)
        changed[path] = (
            _symbols_on_lines(new_symbols, new_source, new_lines) | _symbols_on_lines(old_symbols, old_source, old_lines)
        )
        functions |= {s.name for s in old_symbols + new_symbols if s.is_function}
        new_names = {s.name for s in new_symbols}
        removed += [f"{path}::{s.name}" for s in old_symbols if s.name in changed[path] and s.name not in new_names]

    # Read every Python file at head once.
    sources = {
        path: _read(repo_root, head, path)
        for path in git(repo_root, "ls-tree", "-r", "--name-only", head).splitlines()
        if path.endswith(".py")
    }
    code_files = {path: source for path, source in sources.items() if not is_test_file(path)}
    symbols = [symbol for path, source in code_files.items() for symbol in top_level_symbols(path, source)]

    changed_modules = {module_name(path) for path in changed if not is_test_file(path)}
    dependents = sorted(
        path for path, source in sources.items() if path not in changed and imported_modules(source) & changed_modules
    )

    # Follow references until nothing new is affected: a symbol is affected if it references an affected name.
    affected = {name for path, names in changed.items() if not is_test_file(path) for name in names}
    grew = True
    while grew:
        grew = False
        for symbol in symbols:
            if symbol.name not in affected and symbol.references & affected:
                affected.add(symbol.name)
                grew = True
    affected_routes = sorted({s.route for s in symbols if s.route and s.name in affected})

    changed_tests = {path: sources.get(path, "") for path in changed_files if path.endswith(".py") and is_test_file(path)}
    return ImpactReport(
        changed_files=changed_files,
        affected_routes=affected_routes,
        intent_summary=intent_from_commits(commit_messages(repo_root, merge_base, head)),
        risk_hints=risk_hints(changed, affected_routes, changed_tests, removed, functions),
        changed_symbols=sorted(f"{path}::{name}" for path, names in changed.items() for name in names),
        dependents=dependents,
        relevant_journeys=journeys_hitting(affected_routes, journeys or []),
    )
