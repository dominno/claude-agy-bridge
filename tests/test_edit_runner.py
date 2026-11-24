import json
import sys
import textwrap

import pytest

from agy_bridge import config, runner


CONVERSATION = "d709b409-e2d3-4af6-b84a-02dd0841678f"


@pytest.fixture
def provider(tmp_path):
    path = tmp_path / "agy"
    path.write_text("#!" + sys.executable + "\n" + textwrap.dedent(f"""\
        import json, sys, os, time
        conversation = {CONVERSATION!r}
        if sys.argv[1:] == ['--version']:
            print('1.5.16')
            sys.exit()
        scenario = sys.argv[-1]
        if scenario == 'quota':
            print('AGY_ERROR: {{"status":"RESOURCE_EXHAUSTED","error_code":429,"retryable":true}}',
                  file=sys.stderr)
            sys.exit(3)
        if scenario == 'auth':
            print('AGY_ERROR: {{"canonical_status":"UNAUTHENTICATED","error_code":401}}',
                  file=sys.stderr)
            sys.exit(3)
        init = dict(event='init', init={{}})
        if scenario == 'nested':
            init['init']['conversation_id'] = conversation
        else:
            init['conversation_id'] = conversation
        line = json.dumps(init).encode() + b'\\n'
        if scenario == 'limit':
            os.write(1, line + b'x' * 4096 + b'\\n')
            sys.exit()
        os.write(1, line[:5])
        os.write(1, line[5:])
        if scenario in ('timeout', 'cancel'):
            time.sleep(30)
        if scenario == 'malformed':
            print('bad json')
            sys.exit()
        if scenario == 'permission':
            print('tool was auto-denied', file=sys.stderr)
        assert '--conversation' not in sys.argv or sys.argv[sys.argv.index('--conversation') + 1] == conversation
        result = dict(conversation_id=conversation, status='SUCCESS', response='done')
        if scenario == 'denied result':
            result['denied_actions'] = [{{'action':'write_file'}}]
        if scenario == 'provider timeout':
            result['finished_reason'] = 'print_timeout'
        print(json.dumps(dict(event='result', result=result)), end='')
        """), encoding="utf-8")
    path.chmod(0o700)
    return str(path)


def test_permissions_and_version_gates():
    version = (1, 5, 16)
    read = runner.build_argv(version, "/bin/agy", "a-model")
    settings = json.loads(read[read.index("--settings") + 1])
    assert settings["agentMode"] == ""
    assert settings["allowNonWorkspaceAccess"] is False
    assert "write_file(*)" in settings["permissions"]["deny"]
    write = runner.build_edit_argv(version, "/bin/agy", None, CONVERSATION, "repair", 60, True)
    settings = json.loads(write[write.index("--settings") + 1])
    assert "--mode=accept-edits" in write
    assert write[write.index("--print-timeout") + 1] == "61s"
    assert settings["modelProvider"] == "gemini"
    assert "write_file(*)" not in settings["permissions"]["deny"]
    assert set(settings["permissions"]["deny"]) >= {
        "command(*)", "unsandboxed(*)", "read_url(*)", "execute_url(*)", "mcp(*)",
        "read_file(/home/agy/)", "write_file(/home/agy/)", "read_file(/proc/)", "write_file(/etc/)",
    }
    assert "--settings" not in runner.build_argv((1, 4, 12), "/bin/agy", None)
    with pytest.raises(runner.UnsupportedAgy):
        runner.build_argv((1, 5, 7), "/bin/agy", None)
    with pytest.raises(runner.UnsupportedAgy):
        runner.build_edit_argv((1, 5, 15), "/bin/agy", None, None, "repair", 60)
    with pytest.raises(config.UsageError):
        runner.build_edit_argv(version, "/bin/agy", None, "--untrusted", "repair", 60)


@pytest.mark.parametrize("scenario,code,kind", [
    ("ok", 0, None),
    ("nested", 0, None),
    ("auth", config.EXIT_AUTH, "auth"),
    ("quota", config.EXIT_QUOTA, "quota"),
    ("permission", config.EXIT_SOFT_DENY, "permission"),
    ("denied result", config.EXIT_SOFT_DENY, "permission"),
    ("provider timeout", config.EXIT_TIMEOUT, "timeout"),
    ("timeout", config.EXIT_TIMEOUT, "timeout"),
    ("cancel", 130, "cancelled"),
    ("limit", config.EXIT_INCOMPLETE, "output_limit"),
    ("malformed", config.EXIT_AGY_FAILED, "protocol"),
])
def test_edit_transport(provider, tmp_path, scenario, code, kind):
    ids = []
    wrapped = []

    def wrap(argv):
        wrapped.append(argv)
        return argv

    result = runner.run_edit_agy(
        provider, str(tmp_path), {}, None, .5 if scenario == "timeout" else 5,
        CONVERSATION if scenario == "nested" else None, scenario, ids.append,
        max_out=512, wrap=wrap, should_cancel=lambda: scenario == "cancel" and bool(ids),
    )
    assert wrapped[0] == [provider, "--version"]
    assert wrapped[1][-2:] == ["--print", scenario]
    assert result.exit_code == code
    assert result.error_kind == kind
    assert ids == ([] if scenario in ("auth", "quota") else [CONVERSATION])
    if scenario in ("ok", "nested"):
        assert result.response == "done"
    if scenario == "quota":
        assert result.retryable is True


def test_preflight_version_skips_second_probe(provider, tmp_path):
    commands = []
    result = runner.run_edit_agy(
        provider, str(tmp_path), {}, None, 5, None, "ok",
        version=(1, 5, 16), wrap=lambda args: commands.append(args) or args,
    )
    assert result.exit_code == 0
    assert len(commands) == 1
