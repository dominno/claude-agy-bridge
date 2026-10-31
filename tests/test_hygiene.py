import ast
import json
import os
import re
import stat

import pytest

from conftest import ROOT

ALLOWED_TOOL = "Bash(${CLAUDE_PLUGIN_ROOT}/bin/agy-bridge:*)"
FORBIDDEN_MODULES = {"urllib", "urllib2", "urllib3", "socket", "http", "httplib", "requests", "ftplib", "smtplib",
                     "ssl", "telnetlib", "xmlrpc", "websocket", "websockets", "aiohttp", "httpx", "asyncio"}


def py_files():
    for top in ("src", "bin"):
        for dirpath, _, names in os.walk(os.path.join(ROOT, top)):
            for name in names:
                path = os.path.join(dirpath, name)
                if name.endswith(".py") or dirpath.endswith("bin"):
                    yield path


def trees():
    for path in py_files():
        with open(path) as fh:
            yield path, ast.parse(fh.read())


def test_r9_plugin_json_has_no_hooks_and_no_user_config():
    data = json.load(open(os.path.join(ROOT, ".claude-plugin", "plugin.json")))
    assert "hooks" not in data and "userConfig" not in data
    assert data["name"] == "agy" and data["version"] == "0.1.0"
    assert not os.path.exists(os.path.join(ROOT, "hooks"))


def test_r9_no_hooks_file_or_agent_or_skill_exists_anywhere():
    skip = {".git", ".venv", "__pycache__", ".pytest_cache"}
    for dirpath, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in skip]
        for name in names:
            assert name != "hooks.json", dirpath
        for d in dirs:
            assert d not in ("hooks", "agents", "skills"), os.path.join(dirpath, d)


def network_imports(tree):
    found = []
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        found += [n for n in names if n.split(".")[0] in FORBIDDEN_MODULES]
    return found


def dangerous_calls(tree):
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in ("eval", "exec", "compile", "__import__"):
                bad.append(fn.id)
            if isinstance(fn, ast.Attribute) and fn.attr in ("system", "popen", "spawnl", "spawnlp", "spawnv"):
                bad.append(fn.attr)
            for kw in node.keywords:
                if kw.arg == "shell" and not (isinstance(kw.value, ast.Constant) and kw.value.value is False):
                    bad.append("shell=")
    return bad


def test_r9_no_network_module_is_imported_under_src_or_bin():
    assert [(p, network_imports(t)) for p, t in trees() if network_imports(t)] == []


def test_r9_no_shell_eval_exec_or_os_system():
    assert [(p, dangerous_calls(t)) for p, t in trees() if dangerous_calls(t)] == []


def test_r9_the_real_checks_catch_a_violation():
    bad = ast.parse("import socket\nfrom urllib import request\nimport subprocess, os\n"
                    "subprocess.run('x', shell=True)\neval('1')\nexec('1')\nos.system('x')\nos.popen('x')\n")
    assert network_imports(bad) == ["socket", "urllib"]
    assert dangerous_calls(bad) == ["shell=", "eval", "exec", "system", "popen"]
    clean = ast.parse("import subprocess\nsubprocess.run(['ls'], shell=False)\n")
    assert network_imports(clean) == [] and dangerous_calls(clean) == []


def test_r9_no_url_literals_in_runtime_code():
    for path, tree in trees():
        if path.endswith("scan.py"):  # its strings are sample text for the URL-credentials rule
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert not re.search(r"https?://", node.value), path


def test_r3_runtime_code_never_names_a_widening_flag_except_to_forbid_it():
    for path, tree in trees():
        if path.endswith("runner.py"):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert "--dangerously" not in node.value and "--yolo" not in node.value, path


def test_commands_allowed_tools_are_exactly_the_one_pattern():
    folder = os.path.join(ROOT, "commands")
    names = sorted(os.listdir(folder))
    assert names == ["delegate.md", "doctor.md", "dry-run.md", "queue.md", "status.md"]
    for name in names:
        text = open(os.path.join(folder, name)).read()
        assert text.startswith("---\n")
        front = text.split("---\n")[1]
        fields = dict(line.split(": ", 1) for line in front.strip().splitlines())
        assert fields["description"].strip() and "argument-hint" in fields
        assert fields["allowed-tools"].strip() == ALLOWED_TOOL


def test_manifests_parse_and_have_required_fields():
    plugin = json.load(open(os.path.join(ROOT, ".claude-plugin", "plugin.json")))
    market = json.load(open(os.path.join(ROOT, ".claude-plugin", "marketplace.json")))
    assert {"name", "version", "description"} <= set(plugin)
    assert market["name"] and market["owner"]["name"]
    (entry,) = market["plugins"]
    assert entry["name"] == plugin["name"] and entry["source"] == "./"


def test_entry_point_is_executable_python_with_the_right_shebang():
    path = os.path.join(ROOT, "bin", "agy-bridge")
    assert open(path).readline().strip() == "#!/usr/bin/env python3"
    assert os.stat(path).st_mode & stat.S_IXUSR


def test_fake_agy_is_standalone_and_executable():
    path = os.path.join(ROOT, "tests", "fake_agy", "agy")
    assert open(path).readline().strip() == "#!/usr/bin/env python3"
    assert os.stat(path).st_mode & stat.S_IXUSR


def test_required_files_exist_and_license_is_mit():
    for rel in ("README.md", "SECURITY.md", "LICENSE", "CHANGELOG.md", "pyproject.toml", ".gitignore",
                "docs/CLI.md", "docs/THREAT_MODEL.md", "docs/COMPAT.md"):
        assert os.path.isfile(os.path.join(ROOT, rel)), rel
    lic = open(os.path.join(ROOT, "LICENSE")).read()
    assert "MIT License" in lic and "Dominik Szopa" in lic


def test_gitignore_covers_the_venv_and_scratch():
    text = open(os.path.join(ROOT, ".gitignore")).read().split()
    assert ".venv/" in text and ".agy-bridge/" in text


def test_docs_do_not_claim_verified_agy_behaviour():
    compat = open(os.path.join(ROOT, "docs", "COMPAT.md")).read().lower()
    assert "unverified" in compat
    readme = open(os.path.join(ROOT, "README.md")).read().lower()
    # The README must state exactly how far verification went: one run on one real agy version, fake files only.
    assert "run once against the real" in readme and "fake files only" in readme
    assert "docs/compat.md" in readme
    # And COMPAT must keep a measured section that names the version and still lists what is unverified.
    assert "measured on agy 1.4.3" in compat and "stays unverified" in compat


def test_no_env_file_or_secret_file_is_tracked_in_the_repo():
    for dirpath, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", ".venv", "__pycache__", ".pytest_cache")]
        for name in names:
            assert not name.startswith(".env"), os.path.join(dirpath, name)


def test_python_files_declare_only_stdlib_imports():
    import sys
    stdlib = set(sys.stdlib_module_names) if hasattr(sys, "stdlib_module_names") else None
    if stdlib is None:
        pytest.skip("needs Python 3.10+")
    for path, tree in trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module.split(".")[0]]
            else:
                continue
            for m in mods:
                assert m in stdlib or m == "agy_bridge", (path, m)


def test_allowed_tools_is_described_as_pre_approval_not_a_restriction():
    for rel in ("README.md", "docs/THREAT_MODEL.md"):
        text = open(os.path.join(ROOT, rel)).read().lower()
        assert "pre-approves" in text and "any arguments" in text, rel
    for name in ("delegate.md", "doctor.md", "dry-run.md"):
        text = open(os.path.join(ROOT, "commands", name)).read().lower()
        assert "only pre-approves" in text and "not a restriction" in text, name


def subprocess_violations(tree):
    """Calls into subprocess whose first argument is not an agy executable variable, or that name git."""
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess" \
                and node.func.attr in ("run", "Popen", "call", "check_call", "check_output"):
            first = node.args[0] if node.args else None
            elements = first.elts if isinstance(first, (ast.List, ast.Tuple)) else []
            head = elements[0] if elements else None
            allowed = (isinstance(head, ast.Name) and head.id in ("exe", "argv")) or \
                (isinstance(first, ast.Name) and first.id == "argv")
            if not allowed:
                bad.append("first argument is not the agy executable")
            for sub in ast.walk(first) if first is not None else []:
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and "git" in sub.value.lower():
                    bad.append("names git")
    return bad


def test_r9_subprocess_is_only_imported_where_agy_is_launched():
    importers = set()
    for path, tree in trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(a.name == "subprocess" for a in node.names):
                importers.add(os.path.basename(path))
    assert importers <= {"runner.py", "cli.py"}, importers


def test_r9_every_subprocess_call_starts_agy_and_none_names_git():
    assert [(p, subprocess_violations(t)) for p, t in trees() if subprocess_violations(t)] == []


def test_r9_only_agy_is_ever_looked_up_and_git_is_not_run():
    for path, tree in trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) in (
                    "which",):
                first = node.args[0] if node.args else None
                assert isinstance(first, ast.Constant) and first.value == "agy", (path, ast.dump(node)[:80])
    for path, _ in trees():
        text = open(path).read()
        assert "diff-tree" not in text and "rev-parse" not in text, path
        if not path.endswith("grading.py"):  # explicit scratch-only git apply, never delegate
            assert '"git"' not in text and "'git'" not in text, path


def test_r9_the_subprocess_check_catches_a_git_call():
    bad = ast.parse("import subprocess\nsubprocess.run(['git', 'diff'])\nsubprocess.run([exe, 'x'])\n"
                    "subprocess.run(['ls'])\n")
    assert subprocess_violations(bad) == ["first argument is not the agy executable", "names git",
                                          "first argument is not the agy executable"]
    ok = ast.parse("import subprocess\nsubprocess.run([exe, '--version'])\nsubprocess.Popen(argv)\n")
    assert subprocess_violations(ok) == []


SUBPROCESS_MODULES = {"runner.py", "cli.py"}


def process_violations(tree, filename):
    """Everything that could start another program, other than the two allowed agy launch sites."""
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "subprocess" and filename not in SUBPROCESS_MODULES:
                    bad.append("subprocess imported outside runner/cli")
        elif isinstance(node, ast.ImportFrom) and node.module:
            top = node.module.split(".")[0]
            if top == "subprocess":
                bad.append("from subprocess import")
            if top == "os" and any(a.name.startswith(("exec", "spawn", "posix_spawn", "system", "popen"))
                                   for a in node.names):
                bad.append("from os import exec/spawn")
            if top == "pty" and any(a.name == "spawn" for a in node.names):
                bad.append("from pty import spawn")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value.id if isinstance(node.func.value, ast.Name) else ""
            name = node.func.attr
            if owner == "os" and name.startswith(("exec", "spawn", "posix_spawn", "fork")):
                bad.append("os.%s" % name)
            if owner == "pty" and name in ("spawn", "fork"):
                bad.append("pty.%s" % name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            text = node.value.decode("utf-8", "ignore") if isinstance(node.value, bytes) else node.value
            if os.path.basename(text.strip()).lower() == "git" and filename != "grading.py":
                bad.append("a value naming git")
    return bad


def test_r9_nothing_can_start_another_program_except_the_agy_launch_sites():
    found = [(p, process_violations(t, os.path.basename(p))) for p, t in trees()]
    assert [x for x in found if x[1]] == []


@pytest.mark.parametrize("source,filename,expected", [
    ("from subprocess import run\n", "runner.py", "from subprocess import"),
    ("import subprocess\n", "stage.py", "subprocess imported outside runner/cli"),
    ("import subprocess as sp\n", "report.py", "subprocess imported outside runner/cli"),
    ("import os\nos.execv('/bin/ls', ['ls'])\n", "runner.py", "os.execv"),
    ("import os\nos.execvp('ls', ['ls'])\n", "runner.py", "os.execvp"),
    ("import os\nos.spawnl(0, '/bin/ls')\n", "runner.py", "os.spawnl"),
    ("import os\nos.posix_spawn('/bin/ls', [], {})\n", "runner.py", "os.posix_spawn"),
    ("import pty\npty.spawn(['ls'])\n", "runner.py", "pty.spawn"),
    ("from os import execv\n", "runner.py", "from os import exec/spawn"),
    ("tool = 'git'\n", "stage.py", "a value naming git"),
    ("tool = '/usr/bin/git'\n", "stage.py", "a value naming git"),
    ("tool = b'git'\n", "stage.py", "a value naming git"),
])
def test_r9_the_process_check_fails_on_each_of_these_snippets(source, filename, expected):
    assert expected in process_violations(ast.parse(source), filename)


def test_r9_the_process_check_accepts_the_real_launch_pattern():
    ok = ast.parse("import subprocess\nimport pty\nm, s = pty.openpty()\nsubprocess.Popen(argv)\n"
                   "'a .git entry'\n'.git'\n")
    assert process_violations(ok, "runner.py") == []


def test_scratch_execution_is_only_called_by_explicit_grading_module():
    callers=[]
    for path, tree in trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'run_scratch':
                callers.append(os.path.basename(path))
    assert callers and set(callers) == {'grading.py'}
    from agy_bridge import grading
    assert grading.TIMEOUT == 300
