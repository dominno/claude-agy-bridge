"""Layering of the agy_bridge package: import direction, no cycles, size limits and the one process seam.

Layers (CONTRIBUTING.md, "Code layout"): cli -> app -> (core, port); adapters implement port and may use core;
core imports nothing outside core. Only __init__ (the version) and __main__ stay at the package root; every other
module lives in a layer, and the size limits apply to all of them.
"""
import ast
import os

import pytest

from conftest import SCRIPTS

PACKAGE = os.path.join(SCRIPTS, "agy_bridge")
ROOT_MODULES = {"agy_bridge", "agy_bridge.__main__"}
ALLOWED = {
    "core": {"core"},
    "port": {"core", "port"},
    "adapters": {"core", "port", "adapters"},
    "app": {"core", "port", "app"},
    "cli": {"core", "app", "cli"},
}
# agy_bridge/__init__.py holds only __version__; the app and cli layers may read it.
VERSION_MODULE = "agy_bridge"
# The composition root: the one cli module that binds each port to its adapter. No other cli module may import
# adapters or ports; command handlers reach ports only through app use cases (CONTRIBUTING.md, "Code layout").
COMPOSITION_ROOT = "agy_bridge.cli.wiring"
MAX_MODULE_LINES = 600
MAX_FUNCTION_LINES = 60
# The only module that may import subprocess or start a program another way.
PROCESS_SEAM = "agy_bridge.port.process"
ADAPTER_NAMES = {"agy", "bwrap", "sandbox-exec"}


def _modules():
    found = {}
    for dirpath, dirs, names in os.walk(PACKAGE):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in names:
            if name.endswith(".py"):
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, SCRIPTS)[:-3].replace(os.sep, ".")
                found[rel[:-len(".__init__")] if rel.endswith(".__init__") else rel] = path
    return found


MODULES = _modules()
SOURCES = {name: open(path).read() for name, path in MODULES.items()}
TREES = {name: ast.parse(text) for name, text in SOURCES.items()}


def layer(module):
    parts = module.split(".")
    if module in ROOT_MODULES:
        return "root"
    return parts[1]


def _is_package(module):
    return MODULES[module].endswith("__init__.py")


def _type_checking_only(tree):
    """Import nodes inside `if TYPE_CHECKING:` blocks; they never run."""
    skipped = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and getattr(node.test, "id", getattr(node.test, "attr", "")) == "TYPE_CHECKING":
            for child in node.body:
                skipped.update(id(sub) for sub in ast.walk(child))
    return skipped


def imports(module, top_level_only=False):
    """The package modules `module` imports, resolved to dotted names."""
    tree = TREES[module]
    skipped = _type_checking_only(tree)
    nodes = tree.body if top_level_only else ast.walk(tree)
    base = module.split(".") if _is_package(module) else module.split(".")[:-1]
    found = set()
    for node in nodes:
        if id(node) in skipped:
            continue
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            prefix = base[:len(base) - node.level + 1] if node.level else []
            target = ".".join(prefix + ([node.module] if node.module else []))
            # `from .core import config` imports the module agy_bridge.core.config; `from .stage import x` the module.
            targets = [m for m in ("%s.%s" % (target, alias.name) for alias in node.names) if m in MODULES] or [target]
        else:
            continue
        found.update(name for name in targets if name in MODULES and name != module)
    return found


def test_the_module_map_sees_the_layers():
    assert {"agy_bridge.core.stage", "agy_bridge.core.config", "agy_bridge.port.process"} <= set(MODULES)
    assert {"agy_bridge.cli", "agy_bridge.cli.wiring", "agy_bridge.cli.entry"} <= set(MODULES)
    assert imports("agy_bridge.core.stage") >= {"agy_bridge.core.config", "agy_bridge.core.rootcheck"}


@pytest.mark.parametrize("module", sorted(m for m in MODULES if layer(m) in ALLOWED))
def test_imports_follow_the_dependency_direction(module):
    allowed = ALLOWED[layer(module)] | ({"adapters", "port"} if module == COMPOSITION_ROOT else set())
    wrong = sorted(name for name in imports(module) if layer(name) not in allowed
                   and not (name == VERSION_MODULE and layer(module) in ("app", "cli")))
    assert wrong == [], "%s (%s) may only import %s" % (module, layer(module), sorted(allowed))


def _cycle(graph):
    state = {}

    def visit(node, path):
        state[node] = "open"
        for nxt in sorted(graph.get(node, ())):
            if state.get(nxt) == "open":
                return path[path.index(nxt):] + [nxt]
            if nxt not in state:
                found = visit(nxt, path + [nxt])
                if found:
                    return found
        state[node] = "done"
        return None

    for start in sorted(graph):
        if start not in state:
            found = visit(start, [start])
            if found:
                return found
    return None


def test_module_level_imports_have_no_cycles():
    assert _cycle({m: imports(m, top_level_only=True) for m in MODULES}) is None


def test_layered_modules_have_no_cycles_even_through_deferred_imports():
    layered = [m for m in MODULES if layer(m) in ALLOWED]
    assert _cycle({m: imports(m) & set(layered) for m in layered}) is None


def test_the_cycle_check_finds_a_cycle():
    assert _cycle({"a": {"b"}, "b": {"c"}, "c": {"a"}}) == ["a", "b", "c", "a"]
    assert _cycle({"a": {"b"}, "b": set()}) is None


def test_every_module_is_in_a_layer():
    stray = sorted(m for m in MODULES if layer(m) not in ALLOWED and m not in ROOT_MODULES)
    assert stray == [], "move these into core, port, adapters, app or cli"


@pytest.mark.parametrize("module", sorted(MODULES))
def test_modules_and_functions_stay_small(module):
    assert len(SOURCES[module].splitlines()) <= MAX_MODULE_LINES, module
    long = ["%s: %d lines" % (node.name, node.end_lineno - node.lineno + 1) for node in ast.walk(TREES[module])
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.end_lineno - node.lineno + 1 > MAX_FUNCTION_LINES]
    assert long == [], module


def test_only_the_cli_parses_arguments():
    # argparse belongs to the cli layer: use cases take plain values or the parsed namespace, never build parsers.
    users = sorted(m for m, tree in TREES.items() for node in ast.walk(tree)
                   if (isinstance(node, ast.Import) and any(a.name == "argparse" for a in node.names))
                   or (isinstance(node, ast.ImportFrom) and node.module == "argparse"))
    assert users and all(layer(m) == "cli" for m in users), users


def launches(tree):
    """Every way of starting another program: subprocess, os.exec*/fork/spawn*, pty.fork/spawn."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names if alias.name.split(".")[0] == "subprocess"]
        elif isinstance(node, ast.ImportFrom) and node.module:
            top = node.module.split(".")[0]
            names = [alias.name for alias in node.names]
            if top == "subprocess" or (top == "os" and any(n.startswith(("exec", "fork", "spawn", "posix_spawn"))
                                                           for n in names)) or (top == "pty" and {"fork", "spawn"} & set(names)):
                found.append("from %s import %s" % (node.module, ", ".join(names)))
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "os" and node.attr.startswith(("exec", "fork", "spawn", "posix_spawn")):
                found.append("os." + node.attr)
            if node.value.id == "pty" and node.attr in ("fork", "spawn"):
                found.append("pty." + node.attr)
    return found


def test_only_the_process_seam_starts_programs():
    starters = {module: launches(tree) for module, tree in TREES.items() if launches(tree)}
    assert set(starters) == {PROCESS_SEAM}, starters


def test_only_the_launch_sites_import_the_process_seam():
    # Starting a program through the seam is limited too: tests/test_hygiene.py keeps the allowlist (SEAM_CALLERS)
    # and also rejects aliased and `from ...process import` forms.
    from test_hygiene import SEAM_CALLERS
    callers = {m for m in MODULES if PROCESS_SEAM in imports(m)}
    allowed = {path[:-len(".py")].replace("/", ".") for path in SEAM_CALLERS}
    assert callers and callers <= allowed, sorted(callers - allowed)


@pytest.mark.parametrize("source", ["import subprocess\n", "import subprocess as sp\n", "from subprocess import run\n",
                                    "import os\nos.execv('/bin/ls', ['ls'])\n", "import os\nos.fork()\n",
                                    "import os\nos.posix_spawn('/bin/ls', [], {})\n", "import os\nos.spawnv(0, 'x', [])\n",
                                    "from os import execvp\n", "import pty\npty.fork()\n", "from pty import spawn\n"])
def test_the_launch_check_catches_each_way_of_starting_a_program(source):
    assert launches(ast.parse(source))


def test_core_does_not_name_the_model_runner_or_a_sandbox_tool():
    for module, tree in TREES.items():
        if layer(module) != "core":
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value.strip().lower() not in ADAPTER_NAMES, (module, node.value)
