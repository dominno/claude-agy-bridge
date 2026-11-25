import ast
import json
import os
import re
import stat

import pytest

from conftest import ROOT, SCRIPTS, SKILL

ACTIONS = ('delegate', 'doctor', 'dry-run', 'implement', 'jobs', 'queue', 'status')
# The only bridge subcommands the skill pre-approves; none of them sends a file or changes repository state.
PRE_APPROVED = ('dry-run', 'events', 'doctor', 'jobs list', 'queue add --dry-run', 'queue status', 'status')
# Subcommands that send, start a worker or change state: each must go through the permission prompt.
NEVER_PRE_APPROVED = ('delegate', 'queue add', 'queue run', 'queue watch', 'queue cancel', 'verdict', 'implement',
                      'implement-apply', 'jobs start', 'jobs apply', 'jobs prune', 'jobs remove', 'grade-mutants',
                      'apply-diff')
LAUNCHER = '"<skill>/scripts/agy-bridge"'
FORBIDDEN_MODULES = {"urllib", "urllib2", "urllib3", "socket", "http", "httplib", "requests", "ftplib", "smtplib",
                     "ssl", "telnetlib", "xmlrpc", "websocket", "websockets", "aiohttp", "httpx", "asyncio"}


def py_files():
    for dirpath, dirs, names in os.walk(SCRIPTS):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in names:
            path = os.path.join(dirpath, name)
            if name.endswith(".py") or path == os.path.join(SCRIPTS, "agy-bridge"):
                yield path


def trees():
    for path in py_files():
        with open(path) as fh:
            yield path, ast.parse(fh.read())


def test_r9_plugin_json_has_no_hooks_and_no_user_config():
    data = json.load(open(os.path.join(ROOT, ".claude-plugin", "plugin.json")))
    assert "hooks" not in data and "userConfig" not in data
    from agy_bridge import __version__

    assert data["name"] == "agy" and data["version"] == __version__
    with open(os.path.join(ROOT, 'pyproject.toml')) as project:
        assert re.search(r'^version = "([^"]+)"$', project.read(), re.M)[1] == __version__
    assert not os.path.exists(os.path.join(ROOT, "hooks"))


def test_r9_no_hooks_file_or_agent_and_only_the_one_skill_exists():
    skip = {".git", ".venv", "__pycache__", ".pytest_cache"}
    skill_parents = {os.path.join(ROOT, ".claude", "skills"), os.path.join(ROOT, ".agents", "skills")}
    for dirpath, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in skip]
        for name in names:
            assert name != "hooks.json", dirpath
        for d in dirs:
            if os.path.join(dirpath, d) == os.path.join(SKILL, "agents"):
                continue  # Codex skill metadata, checked below
            assert d not in ("hooks", "agents", "commands"), os.path.join(dirpath, d)
            if d == "skills":
                assert os.path.join(dirpath, d) in skill_parents, os.path.join(dirpath, d)
    for parent in skill_parents:
        assert sorted(os.listdir(parent)) == ["agy-bridge"], parent
    assert not os.path.islink(SKILL) and os.path.isfile(os.path.join(SKILL, "SKILL.md"))
    link = os.path.join(ROOT, ".agents", "skills", "agy-bridge")
    assert os.path.islink(link) and os.readlink(link) == "../../.claude/skills/agy-bridge"
    assert os.path.realpath(link) == os.path.realpath(SKILL)
    for old in ("bin", "src", "commands", "docs"):
        assert not os.path.exists(os.path.join(ROOT, old)), old


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
        if path.endswith(os.path.join("adapters", "agy.py")):  # the agy adapter names them only to forbid them
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert "--dangerously" not in node.value and "--yolo" not in node.value, path


def _skill():
    text = open(os.path.join(SKILL, "SKILL.md")).read()
    assert text.startswith("---\n")
    _, front, body = text.split("---\n", 2)
    return dict(line.split(": ", 1) for line in front.strip().splitlines()), body


def _section(name):
    """The body of one `## <action>` section of SKILL.md."""
    _, body = _skill()
    parts = re.split(r"^## ", body, flags=re.M)
    (section,) = [p for p in parts if p.split("\n", 1)[0].strip() == name]
    return section


def test_skill_frontmatter_names_every_action():
    fields, body = _skill()
    assert fields["name"] == "agy-bridge" == os.path.basename(SKILL)
    assert fields["description"].strip() and len(fields["description"]) <= 1024
    hint = fields["argument-hint"]
    for action in ACTIONS:
        assert action in hint, action
        assert re.search(r"^## %s$" % re.escape(action), body, re.M), action
        assert "| `%s` |" % action in body, action


def test_skill_preapproval_is_scoped_to_read_only_subcommands():
    fields, _ = _skill()
    assert fields["allowed-tools"] == " ".join(
        "Bash(${CLAUDE_SKILL_DIR}/scripts/agy-bridge " + cmd + " *) "
        'Bash("${CLAUDE_SKILL_DIR}/scripts/agy-bridge" ' + cmd + " *)" for cmd in PRE_APPROVED)
    rules = re.findall(r"Bash\(\"?\$\{CLAUDE_SKILL_DIR\}/scripts/agy-bridge\"? ([^)]*?) \*\)", fields["allowed-tools"])
    assert len(rules) == 2 * len(PRE_APPROVED)
    for rule in rules:
        for sending in NEVER_PRE_APPROVED:
            assert rule != sending and not rule.startswith(sending + " ") or rule == "queue add --dry-run", rule


def test_the_skill_is_user_only_because_it_can_send_files():
    fields, _ = _skill()
    assert fields.get("disable-model-invocation") == "true"
    # Codex reads its own metadata file: no implicit invocation, only an explicit $agy-bridge.
    assert os.listdir(os.path.join(SKILL, "agents")) == ["openai.yaml"]
    codex = open(os.path.join(SKILL, "agents", "openai.yaml")).read()
    assert re.search(r"^policy:\n  allow_implicit_invocation: false$", codex, re.M)


def test_every_body_invocation_of_the_bridge_is_quoted():
    _, body = _skill()
    assert body.count(LAUNCHER) >= 20
    for match in re.finditer(r"scripts/agy-bridge", body):
        before = body[max(0, match.start() - len('"<skill>/')):match.start()]
        assert before == '"<skill>/' and body[match.end():match.end() + 1] == '"', match.start()
    flat = " ".join(body.split())
    assert "absolute path of the directory that holds this SKILL.md" in flat
    assert "never hide it in a shell variable" in flat
    assert "CLAUDE_PLUGIN_ROOT" not in body and "bin/agy-bridge" not in body


SENDING_ACTIONS = ("delegate", "queue", "implement")


@pytest.mark.parametrize("name", SENDING_ACTIONS)
def test_sending_actions_wait_for_user_confirmation(name):
    flat = " ".join(_section(name).split())
    assert "STOP" in flat and "only after the user confirms" in flat, name


def test_delegate_runs_the_real_call_only_after_confirmation():
    body = _section("delegate")
    assert body.index("dry-run --repo") < body.index("STOP") < body.index("delegate --repo")
    assert "Run the real delegate only after the user confirms" in " ".join(body.split())


def test_queue_confirms_a_real_add_while_a_watcher_runs_and_before_run():
    flat = " ".join(_section("queue").split())
    assert flat.index("status --repo") < flat.index("STOP, ask, and run the real `queue add` only after the user")
    assert "run `queue run` or `queue watch` only after the user confirms" in flat
    assert "jobs list -C" in flat and "including jobs other sessions added" in flat


def test_implement_confirms_before_launch_and_apply_checks_the_hash():
    flat = " ".join(_section("implement").split())
    assert flat.index("implement --dry-run") < flat.index("STOP") < flat.index("implement --repo")
    assert "launch only after the user confirms" in flat
    assert "implement-apply <id> --sha256 <reviewed hash>" in flat


def test_skill_references_stay_one_level_deep_and_exist():
    _, body = _skill()
    links = re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", body)
    assert links
    for link in links:
        assert link.startswith("references/") and link.count("/") == 1, link
        assert os.path.isfile(os.path.join(SKILL, link)), link
    refs = os.path.join(SKILL, "references")
    assert all(os.path.isfile(os.path.join(refs, name)) for name in os.listdir(refs))
    assert len(open(os.path.join(SKILL, "SKILL.md")).read().splitlines()) <= 300


def test_skill_folder_is_self_contained():
    for rel in ("SKILL.md", "README.md", "LICENSE", "scripts/agy-bridge", "scripts/agy_bridge/cli/__init__.py"):
        assert os.path.isfile(os.path.join(SKILL, rel)), rel
    assert open(os.path.join(SKILL, "LICENSE")).read() == open(os.path.join(ROOT, "LICENSE")).read()


def test_manifests_parse_and_have_required_fields():
    plugin = json.load(open(os.path.join(ROOT, ".claude-plugin", "plugin.json")))
    market = json.load(open(os.path.join(ROOT, ".claude-plugin", "marketplace.json")))
    assert {"name", "version", "description"} <= set(plugin)
    assert market["name"] and market["owner"]["name"]
    (entry,) = market["plugins"]
    assert entry["name"] == plugin["name"] and entry["source"] == "./"
    # Both point at the one skill folder; the plugin has no commands/ or bin/ of its own.
    assert entry["skills"] == plugin["skills"] == ["./.claude/skills/agy-bridge"]


def test_entry_point_is_executable_python_with_the_right_shebang():
    path = os.path.join(SCRIPTS, "agy-bridge")
    assert open(path).readline().strip() == "#!/usr/bin/env python3"
    assert os.stat(path).st_mode & stat.S_IXUSR


def test_fake_agy_is_standalone_and_executable():
    path = os.path.join(ROOT, "tests", "fake_agy", "agy")
    assert open(path).readline().strip() == "#!/usr/bin/env python3"
    assert os.stat(path).st_mode & stat.S_IXUSR


def test_required_files_exist_and_license_is_mit():
    for rel in ("README.md", "SECURITY.md", "LICENSE", "CHANGELOG.md", "pyproject.toml", ".gitignore",
                ".claude/skills/agy-bridge/references/CLI.md", ".claude/skills/agy-bridge/references/THREAT_MODEL.md",
                ".claude/skills/agy-bridge/references/COMPAT.md"):
        assert os.path.isfile(os.path.join(ROOT, rel)), rel
    lic = open(os.path.join(ROOT, "LICENSE")).read()
    assert "MIT License" in lic and "Dominik Szopa" in lic


def test_gitignore_covers_the_venv_and_scratch():
    text = open(os.path.join(ROOT, ".gitignore")).read().split()
    assert ".venv/" in text and ".agy-bridge/" in text


def test_docs_do_not_claim_verified_agy_behaviour():
    compat = open(os.path.join(SKILL, "references", "COMPAT.md")).read().lower()
    assert "unverified" in compat
    readme = open(os.path.join(ROOT, "README.md")).read().lower()
    # The README must state exactly how far verification went: one run on one real agy version, fake files only.
    assert "run once against the real" in readme and "fake files only" in readme
    assert ".claude/skills/agy-bridge/references/compat.md" in readme
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
    for rel in ("README.md", ".claude/skills/agy-bridge/references/THREAT_MODEL.md"):
        text = open(os.path.join(ROOT, rel)).read().lower()
        assert "pre-approves" in text and "listed bridge subcommands" in text, rel
    _, body = _skill()
    text = " ".join(body.lower().split())
    assert "only pre-approves the listed bridge subcommands" in text and "not a restriction" in text
    assert "the permission prompt is the hard stop" in text


LAUNCHERS = {"subprocess": ("run", "Popen", "call", "check_call", "check_output"),
             "process": ("spawn", "run")}  # process: the one process seam, agy_bridge/port/process.py


def subprocess_violations(tree):
    """Launch calls (subprocess or the process seam) whose first argument is not an argv variable, or that name git."""
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and isinstance(node.func.value, ast.Name) and node.func.value.id in LAUNCHERS \
                and node.func.attr in LAUNCHERS[node.func.value.id]:
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


def test_r9_subprocess_imports_are_limited_to_explicit_launchers():
    importers = set()
    for path, tree in trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(a.name == "subprocess" for a in node.names):
                importers.add(_rel(path))
    assert importers <= SUBPROCESS_MODULES, importers


def test_r9_every_subprocess_call_takes_an_explicit_argv():
    assert [(p, subprocess_violations(t)) for p, t in trees() if subprocess_violations(t)] == []


def test_r9_git_is_scoped_to_explicit_implementation_and_grading():
    for path, tree in trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) in (
                    "which",):
                first = node.args[0] if node.args else None
                assert isinstance(first, ast.Constant) and first.value == "agy", (path, ast.dump(node)[:80])
    for path, _ in trees():
        text = open(path).read()
        if _rel(path) not in GIT_MODULES:
            assert "diff-tree" not in text and "rev-parse" not in text, path
            assert '"git"' not in text and "'git'" not in text, path


def test_r9_the_subprocess_check_catches_a_git_call():
    bad = ast.parse("import subprocess\nsubprocess.run(['git', 'diff'])\nsubprocess.run([exe, 'x'])\n"
                    "subprocess.run(['ls'])\n")
    assert subprocess_violations(bad) == ["first argument is not the agy executable", "names git",
                                          "first argument is not the agy executable"]
    ok = ast.parse("import subprocess\nsubprocess.run([exe, '--version'])\nsubprocess.Popen(argv)\n")
    assert subprocess_violations(ok) == []
    seam = ast.parse("process.spawn(['git', 'diff'])\nprocess.run(['ls'])\nprocess.spawn(argv)\nprocess.run(argv)\n")
    assert subprocess_violations(seam) == ["first argument is not the agy executable", "names git",
                                           "first argument is not the agy executable"]


# Paths relative to scripts/. Only the process seam imports subprocess; only the launch sites import the seam,
# always as `from <..>port import process` with no alias, so every launch call reads `process.spawn/run(...)`.
GIT_MODULES = {"agy_bridge/app/grading.py", "agy_bridge/adapters/workspace.py", "agy_bridge/adapters/edit_apply.py"}
SUBPROCESS_MODULES = {"agy_bridge/port/process.py"}
# The launch sites: only adapters start programs. runner.py became adapters/agy.py and (run_scratch) part of
# adapters/sandbox.py; the old cli.py and implement.py worker launches moved to adapters/launcher.py.
# port/model_runner.py only re-exports TimeoutExpired so use cases never import the seam. The list may only shrink.
SEAM_CALLERS = {"agy_bridge/adapters/agy.py", "agy_bridge/adapters/sandbox.py", "agy_bridge/adapters/workspace.py",
                "agy_bridge/adapters/edit_sandbox.py", "agy_bridge/adapters/launcher.py",
                "agy_bridge/port/model_runner.py"}
SEAM = "agy_bridge.port.process"


def _rel(path):
    return os.path.relpath(path, SCRIPTS).replace(os.sep, "/")


def _resolve(node, filename):
    """The absolute dotted module an ImportFrom names, resolved against filename (relative to scripts/)."""
    if not node.level:
        return node.module or ""
    parts = filename[:-len(".py")].split("/")
    base = parts if parts[-1] == "__init__" else parts[:-1]
    base = [p for p in base if p != "__init__"]
    prefix = base[:len(base) - node.level + 1]
    return ".".join(prefix + ([node.module] if node.module else []))


def seam_violations(tree, filename):
    """Imports of the process seam outside SEAM_CALLERS, and seam imports that hide the `process.` prefix."""
    bad = []
    if filename in SUBPROCESS_MODULES:
        return bad
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == SEAM or alias.name.startswith(SEAM + "."):
                    bad.append("process seam imported by name")
                    if filename not in SEAM_CALLERS:
                        bad.append("process seam imported outside the launch sites")
        elif isinstance(node, ast.ImportFrom):
            target = _resolve(node, filename)
            if target == SEAM or target.startswith(SEAM + "."):
                bad.append("from process seam import")
                if filename not in SEAM_CALLERS:
                    bad.append("process seam imported outside the launch sites")
            elif target == SEAM.rsplit(".", 1)[0]:
                for alias in node.names:
                    if alias.name in ("process", "*"):
                        if alias.asname or alias.name == "*":
                            bad.append("aliased process seam import")
                        if filename not in SEAM_CALLERS:
                            bad.append("process seam imported outside the launch sites")
    return bad


def process_violations(tree, filename):
    """Unexpected launch mechanisms or executables outside the explicit workflow modules."""
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
            if os.path.basename(text.strip()).lower() == "git" and filename not in GIT_MODULES:
                bad.append("a value naming git")
    return bad


def test_r9_nothing_can_start_another_program_except_the_agy_launch_sites():
    found = [(p, process_violations(t, _rel(p))) for p, t in trees()]
    assert [x for x in found if x[1]] == []


def test_r9_only_the_launch_sites_import_the_process_seam():
    found = [(_rel(p), seam_violations(t, _rel(p))) for p, t in trees()]
    assert [x for x in found if x[1]] == []
    importers = {_rel(p) for p, t in trees() for node in ast.walk(t)
                 if isinstance(node, ast.ImportFrom) and _resolve(node, _rel(p)) == "agy_bridge.port"
                 and any(a.name == "process" for a in node.names)}
    assert importers and importers <= SEAM_CALLERS, importers


@pytest.mark.parametrize("source,filename,expected", [
    ("from ..port import process as p\np.spawn(['sh', '-c', 'x'])\n", "agy_bridge/app/report.py",
     "process seam imported outside the launch sites"),
    ("from ..port import process as p\np.spawn(['sh', '-c', 'x'])\n", "agy_bridge/app/report.py",
     "aliased process seam import"),
    ("from ..port.process import run\nrun(['ls'])\n", "agy_bridge/app/report.py", "from process seam import"),
    ("from ..port.process import run\nrun(['ls'])\n", "agy_bridge/app/report.py",
     "process seam imported outside the launch sites"),
    ("from ..port import process\nprocess.spawn(argv)\n", "agy_bridge/app/report.py",
     "process seam imported outside the launch sites"),
    ("from ..port import process\nprocess.spawn(argv)\n", "agy_bridge/cli/entry.py",
     "process seam imported outside the launch sites"),
    ("from ..port import process\nprocess.spawn(argv)\n", "agy_bridge/app/stats.py",
     "process seam imported outside the launch sites"),
    ("from ..port.process import spawn\n", "agy_bridge/adapters/agy.py", "from process seam import"),
    ("from ..port.process import spawn\n", "agy_bridge/adapters/sandbox.py", "from process seam import"),
    ("from ..port import process as p\n", "agy_bridge/adapters/agy.py", "aliased process seam import"),
    ("from ..port import *\n", "agy_bridge/app/verdicts.py", "process seam imported outside the launch sites"),
    ("from agy_bridge.port import process\n", "agy_bridge/app/queue.py", "process seam imported outside the launch sites"),
    ("import agy_bridge.port.process as p\n", "agy_bridge/adapters/jobstore.py", "process seam imported by name"),
    ("import agy_bridge.port.process\n", "agy_bridge/adapters/jobstore.py",
     "process seam imported outside the launch sites"),
    ("from . import process\n", "agy_bridge/port/job_store.py", "process seam imported outside the launch sites"),
])
def test_r9_the_seam_check_fails_on_each_of_these_snippets(source, filename, expected):
    assert expected in seam_violations(ast.parse(source), filename)


def test_r9_the_seam_check_accepts_the_real_launch_pattern():
    ok = ast.parse("from ..port import process, safety\nprocess.spawn(argv)\nprocess.run([exe, '--version'])\n")
    assert seam_violations(ok, "agy_bridge/adapters/agy.py") == []
    assert seam_violations(ast.parse("from . import process\n"), "agy_bridge/port/model_runner.py") == []
    assert seam_violations(ast.parse("from ..port import safety\n"), "agy_bridge/app/report.py") == []
    assert seam_violations(ast.parse("import subprocess\n"), "agy_bridge/port/process.py") == []


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
    assert process_violations(ok, "agy_bridge/port/process.py") == []


def test_scratch_execution_is_only_called_by_explicit_grading_module():
    callers=[]
    for path, tree in trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'run_scratch':
                callers.append(os.path.basename(path))
    assert callers and set(callers) == {'grading.py'}
    from agy_bridge.app import grading
    assert grading.TIMEOUT == 300


def test_threat_model_states_the_implement_agent_shares_the_host_network():
    text = open(os.path.join(SKILL, "references", "THREAT_MODEL.md")).read()
    assert "share-net" in text and "metadata" in text and "169.254.169.254" in text
    assert "share-net" in open(os.path.join(SKILL, "references", "IMPLEMENT.md")).read()
    assert "share-net" in _section("implement")
