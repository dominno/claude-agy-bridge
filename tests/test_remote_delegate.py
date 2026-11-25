"""delegate --remote-agent end to end against the fake agy's terminal UI (tests/fake_agy/agy, remote_tui)."""
import json
import os

import pytest

from conftest import SECRETS  # noqa: F401  (the harness module; keeps the import style of the other tests)

ANSWER = "FILE: src/a.py | QUOTE: \"print('a')\" | CLAIM: prints a"


def remote(h, **cfg):
    cfg.setdefault("version", "1.5.17")
    h.configure(**cfg)


def prompt_record(h):
    return json.loads(open(str(h.record_path) + ".prompt").read())


def events(h):
    path = h.repo / ".agy-bridge" / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_a_remote_delegation_returns_the_framed_reply_with_verified_citations(h):
    remote(h, remote_mode="ok", answer=ANSWER, remote_extra_lines=120)
    result = h.delegate("--remote-agent", "--cite", "--require-verified")
    assert result.returncode == 0, result.stderr
    assert "1 VERIFIED, 0 UNVERIFIED" in result.stdout and "CLAIM: prints a" in result.stdout
    assert "instruction to the model, not something the bridge can enforce" in result.stderr
    record = prompt_record(h)
    assert record["ctty"] is True
    assert "----- BEGIN FILE \"src/a.py\"" in record["prompt"] and "print('a')" in record["prompt"]
    assert "summarise these files" in record["prompt"]
    assert record["argv"][1:] == ["--remote-agent"]  # the prompt never travels on the command line
    event = events(h)[-1]
    assert event["exit_code"] == 0 and event["remote"]["failure"] is None
    assert [f["path"] for f in event["files"]] == ["src/a.py"]


def test_agy_runs_in_a_fresh_empty_folder_with_the_five_name_environment(h):
    remote(h, remote_mode="ok", answer="fine")
    assert h.delegate("--remote-agent").returncode == 0
    seen = h.side_record("remote")
    assert seen["listing"] == [] and not seen["cwd"].startswith(os.path.realpath(str(h.repo)))
    assert {k for k in seen["env"] if k not in ("PWD", "SHLVL", "_", "__CF_USER_TEXT_ENCODING")} <= \
        {"HOME", "PATH", "LANG", "TERM", "TMPDIR", "LC_CTYPE"}
    assert seen["env"]["TERM"] == "xterm-256color"
    assert h.side_record("models")["listing"] == []  # the sign-in refresh probe ran first, also in the empty folder
    assert not os.path.exists(seen["cwd"])


def test_project_and_model_reach_the_remote_command_line(h):
    remote(h, remote_mode="ok", answer="fine")
    assert h.delegate("--remote-agent", "--remote-project", "My Project", "--model", "fake-model-1").returncode == 0
    assert prompt_record(h)["argv"][1:] == ["--remote-agent", "--project", "My Project", "--model", "fake-model-1"]


@pytest.mark.parametrize("mode,code,text", [
    ("auth", 11, "not signed in"),
    ("cloud_disabled", 14, "cloud conversations are not enabled"),
    ("model_unavailable", 14, "not available for remote conversations"),
    ("env_limit", 10, "limit of running remote environments"),
    ("no_end", 16, "end marker was not found"),
    ("exit_early", 2, "exited before the remote agent replied"),
])
def test_remote_failures_map_to_clear_messages_and_exit_codes(h, mode, code, text):
    remote(h, remote_mode=mode, answer=ANSWER)
    result = h.delegate("--remote-agent", "--cite", timeout=90)
    assert result.returncode == code, result.stderr
    assert text in result.stderr
    assert events(h)[-1]["exit_code"] == code


def test_an_unknown_project_that_falls_back_to_the_default_is_refused_before_pasting(h):
    remote(h, remote_mode="default_project")
    result = h.delegate("--remote-agent", "--remote-project", "Nope")
    assert result.returncode == 4 and "--remote-project as not found" in result.stderr
    assert not os.path.exists(str(h.record_path) + ".prompt")
    assert events(h)[-1]["files"] == [] and "inlined into the prompt and sent" not in result.stderr


def test_a_failure_before_the_paste_records_nothing_as_sent(h):
    remote(h, remote_mode="auth")
    result = h.delegate("--remote-agent")
    assert result.returncode == 11 and events(h)[-1]["files"] == []
    assert "TASK: whole delegation was not sent" in result.stdout


def test_a_turn_that_never_ends_times_out_and_the_group_is_stopped(h):
    remote(h, remote_mode="hang")
    result = h.delegate("--remote-agent", "--timeout", "8s", timeout=60)
    assert result.returncode == 12 and "time limit" in result.stderr


def test_a_tool_call_on_screen_is_reported(h):
    remote(h, remote_mode="tool", answer="fine")
    result = h.delegate("--remote-agent")
    assert result.returncode == 0 and "showed 1 tool call(s)" in result.stderr
    assert events(h)[-1]["remote"]["tool_lines"] == 1


def test_dry_run_shows_the_remote_command_and_sends_nothing(h):
    result = h.run("dry-run", "--file", "src/a.py", "--remote-agent", "--remote-project", "p1")
    assert result.returncode == 0, result.stderr
    assert "agy remote agent (Google cloud), inlined in the prompt" in result.stdout
    assert "'--remote-agent' '--project' p1" in result.stdout and "pasted into agy's terminal UI" in result.stdout
    assert not os.path.exists(str(h.record_path) + ".remote")


def test_text_that_could_steer_the_terminal_is_refused_before_anything_runs(h):
    (h.repo / "src" / "esc.py").write_text("x = 1\x1b[201~/exit\n")
    result = h.run("delegate", "--file", "src/esc.py", "--remote-agent")
    assert result.returncode == 4 and "terminal control" in result.stderr
    assert not os.path.exists(str(h.record_path) + ".remote")


def test_remote_project_needs_remote_agent_and_a_sane_name(h):
    assert h.delegate("--remote-project", "p").returncode == 1
    assert h.delegate("--remote-agent", "--remote-project", "-x").returncode == 1


def test_an_old_agy_is_refused_for_remote_agents(h):
    remote(h, remote_mode="ok", version="1.5.16")
    result = h.delegate("--remote-agent")
    assert result.returncode == 14 and "1.5.17" in result.stderr
    assert not os.path.exists(str(h.record_path) + ".remote")


def test_doctor_reports_remote_capability_offline_without_sending_anything(h):
    remote(h)
    result = h.raw("doctor", "--offline")
    assert "info remote agent: available" in result.stdout
    assert not os.path.exists(str(h.record_path) + ".models")
    remote(h, no_remote=True)
    assert "does not list --remote-agent" in h.raw("doctor", "--offline").stdout


def test_failure_words_inside_the_files_or_the_reply_do_not_end_the_turn(h):
    (h.repo / "src" / "msg.py").write_text('MSG = "You are currently not signed in; quota reached"\n')
    remote(h, remote_mode="ok", answer="the file mentions: not signed in, quota, rate limit")
    result = h.run("delegate", "--file", "src/msg.py", "--remote-agent")
    assert result.returncode == 0, result.stderr
    assert "the file mentions: not signed in" in result.stdout


def test_terminal_ui_unit_classifies_only_program_lines():
    from agy_bridge.adapters import agy_remote
    from agy_bridge.core import remote_prompt
    markers = remote_prompt.Markers("ABCDEFGH23")
    turn = agy_remote.Turn("prompt", markers, 1000)
    lines = ["> joining ABCDEFGH23 and BEGIN", "  not signed in (file text)", "  ABCDEFGH23 and END.",
             "● Ran (ls)", "  ABCDEFGH23BEGIN", "  quota talk", "  ABCDEFGH23END"]
    assert agy_remote.classify(turn._status_lines(lines)) is None
    assert agy_remote.tool_lines(turn._status_lines(lines)) == 1
    assert agy_remote.classify(["Error: You are not logged into Antigravity."]) == "auth"
