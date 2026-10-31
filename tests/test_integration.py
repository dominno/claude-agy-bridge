import json
import os
import signal
import stat
import subprocess
import sys
import time

import pytest

from conftest import BIN, SECRETS


# ---------- happy path and R2 / R3 / R4 ----------

def test_delegate_returns_the_answer_with_a_header(h):
    h.configure(answer="the answer is 42")
    r = h.delegate()
    assert r.returncode == 0, r.stderr
    assert "the answer is 42" in r.stdout and "files: 1" in r.stdout and "Verify it" in r.stdout


# The macOS Python runtime adds this one name to its own process (shown by `env -i python3`), so the
# Python-based fake sees it even though the bridge never passed it. The test below uses /usr/bin/env.
_ADDED_BY_MACOS_PYTHON = {"__CF_USER_TEXT_ENCODING"}


def test_r2_env_has_only_allowed_names(h):
    r = h.delegate(extra_env=SECRETS)
    assert r.returncode == 0, r.stderr
    assert set(h.record()["env"]) - _ADDED_BY_MACOS_PYTHON == {"HOME", "PATH", "LANG", "TERM", "TMPDIR"}


def test_r2_run_agy_hands_exactly_the_five_names_to_a_plain_program(monkeypatch, tmp_path):
    from agy_bridge import config, runner
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    res = runner.run_agy(["/usr/bin/env"], str(tmp_path), config.build_agy_env(), 10, 100000)
    names = {ln.split("=", 1)[0] for ln in res.out.decode().replace("\r", "").splitlines() if "=" in ln}
    assert names == {"HOME", "PATH", "LANG", "TERM", "TMPDIR"}


def test_r2_secrets_exported_in_the_caller_never_reach_agy(h):
    r = h.delegate(extra_env=SECRETS)
    blob = json.dumps(h.record()["env"])
    assert r.returncode == 0
    for name, value in SECRETS.items():
        assert name not in blob and value not in blob


def test_r2_fails_if_the_allowed_list_is_widened(monkeypatch):
    from agy_bridge import config
    monkeypatch.setenv("SOMETHING_ELSE", "x")
    assert set(config.build_agy_env()) == set(config.ALLOWED_ENV_NAMES)


def test_r2_missing_variables_get_safe_defaults(monkeypatch):
    from agy_bridge import config
    for name in config.ALLOWED_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    env = config.build_agy_env()
    assert set(env) == set(config.ALLOWED_ENV_NAMES) and all(env.values())


def test_r3_argv_has_no_permission_widening_flag(h):
    r = h.delegate()
    argv = h.record()["argv"][1:]
    assert r.returncode == 0
    joined = " ".join(argv).lower()
    for bad in ("--dangerously-skip-permissions", "--yolo", "skip-permission", "dangerous"):
        assert bad not in joined
    assert argv[0] == "--print"


def test_r3_argv_builder_rejects_a_widening_flag(monkeypatch):
    from agy_bridge import runner
    spec = runner.ArgvSpec("--print", "--model", ("--dangerously-skip-permissions",))
    monkeypatch.setitem(runner.ARGV_SPECS, 1, spec)
    with pytest.raises(runner.UnsupportedAgy):
        runner.build_argv((1, 0, 0), "/x/agy", None)
    monkeypatch.setitem(runner.ARGV_SPECS, 1, runner.ArgvSpec("--print", "--model", ("--yolo",)))
    with pytest.raises(runner.UnsupportedAgy):
        runner.build_argv((1, 0, 0), "/x/agy", None)


def test_r3_model_name_is_passed(h):
    assert h.delegate("--model", "fake-model-1").returncode == 0
    assert h.record()["argv"][-2:] == ["--model", "fake-model-1"]
    assert h.delegate("--model", "--yolo").returncode == 1


def test_r3_unknown_agy_version_fails_closed_with_14(h):
    h.configure(version="9.0.0")
    r = h.delegate()
    assert r.returncode == 14
    assert not h.record_path.exists()  # agy was never run for the task


def test_r4_cwd_is_staging_never_the_repo(h):
    r = h.delegate()
    rec = h.record()
    assert r.returncode == 0
    assert rec["cwd"] != os.path.realpath(h.repo) and not rec["cwd"].startswith(os.path.realpath(h.repo))
    assert os.path.basename(rec["cwd"]).startswith("agy-bridge-")
    assert rec["cwd"].startswith(os.path.realpath(h.tmpdir))


def test_r4_staging_holds_only_the_manifest_and_task(h):
    (h.repo / "other.py").write_text("x = 1\n")
    h.delegate()
    assert h.record()["listing"] == ["TASK.md", "src", "src/a.py"]


def test_r4_staging_is_mode_0700_and_agy_runs_in_a_pty(h):
    h.delegate()
    rec = h.record()
    assert rec["cwd_mode"] == 0o700 and rec["stdout_isatty"] is True


def test_r4_staged_content_matches_the_repo_file(h):
    import hashlib
    h.delegate()
    want = hashlib.sha256((h.repo / "src" / "a.py").read_bytes()).hexdigest()
    assert h.record()["digests"]["src/a.py"] == want


# ---------- cleanup ----------

def test_cleanup_after_success(h):
    h.delegate()
    assert h.leftovers() == []


def test_cleanup_after_agy_error(h):
    h.configure(mode="exit", exit_code=3)
    assert h.delegate().returncode == 2
    assert h.leftovers() == []


def test_cleanup_after_a_refusal(h):
    (h.repo / ".env").write_text("A=1\n")
    assert h.run("delegate", "--file", ".env").returncode == 4
    assert h.leftovers() == []


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_cleanup_after_timeout_and_the_whole_group_is_killed(h):
    h.configure(mode="timeout")
    started = time.time()
    r = h.delegate("--timeout", "2s")
    assert r.returncode == 12 and time.time() - started < 20
    assert h.leftovers() == []
    pid = h.record()["child_pid"]
    for _ in range(40):
        if not _alive(pid):
            break
        time.sleep(0.1)
    assert not _alive(pid)


def test_cleanup_after_sigint(h):
    h.configure(mode="timeout")
    proc = subprocess.Popen([sys.executable, BIN, "delegate", *["--repo", str(h.repo), "--file", "src/a.py", "--",
                                                                "task"]],
                            env=h.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for _ in range(100):
        if h.record_path.exists():
            break
        time.sleep(0.1)
    assert h.record_path.exists(), "fake agy never started"
    staging = h.record()["cwd"]
    assert os.path.isdir(staging)
    proc.send_signal(signal.SIGINT)
    proc.communicate(timeout=30)
    assert proc.returncode == 130
    assert not os.path.exists(staging) and h.leftovers() == []
    for _ in range(40):
        if not _alive(h.record()["child_pid"]):
            break
        time.sleep(0.1)
    assert not _alive(h.record()["child_pid"])


def test_cleanup_after_sigterm(h):
    h.configure(mode="timeout")
    proc = subprocess.Popen([sys.executable, BIN, "delegate", "--repo", str(h.repo), "--file", "src/a.py", "--",
                             "task"], env=h.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for _ in range(100):
        if h.record_path.exists():
            break
        time.sleep(0.1)
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=30)
    assert h.leftovers() == []


# ---------- exit codes ----------

@pytest.mark.parametrize("cfg,code", [
    ({"mode": "empty"}, 3),
    ({"mode": "exit", "exit_code": 7}, 2),
    ({"mode": "soft_denial"}, 15),
    ({"mode": "quota"}, 10),
    ({"mode": "auth"}, 11),
    ({"mode": "ok"}, 0),
])
def test_exit_code_table(h, cfg, code):
    h.configure(**cfg)
    assert h.delegate().returncode == code


def test_exit_13_when_agy_is_missing(h):
    os.remove(h.bindir / "agy")
    r = h.run("delegate", "--file", "src/a.py", extra_env={"PATH": "/nonexistent"}, with_fake=False)
    assert r.returncode == 13


def test_exit_1_on_usage_errors(h):
    assert h.run("delegate", "--file", "src/a.py", task="").returncode == 1
    assert h.run("delegate", "--file", "src/a.py", "--timeout", "soon").returncode == 1
    assert h.run("delegate").returncode == 1  # no inputs
    assert subprocess.run([sys.executable, BIN, "delegate", "--bogus"], capture_output=True,
                          env=h.env()).returncode == 1


def test_exit_4_on_refusal_with_every_problem_listed(h):
    (h.repo / ".env").write_text("A=1\n")
    r = h.run("delegate", "--file", ".env", "--file", "../x")
    assert r.returncode == 4 and "2 problem" in r.stderr
    assert not h.record_path.exists()


def test_failure_messages_are_plain_words(h):
    h.configure(mode="empty")
    r = h.delegate()
    assert "Traceback" not in r.stderr and "{" not in r.stderr and "Error(" not in r.stderr


# ---------- output (R8) ----------

def test_oversize_output_is_capped(h):
    h.configure(mode="oversize", oversize_bytes=600000)
    r = h.delegate("--max-out-bytes", "50000")
    assert r.returncode == 16 and "cut at the output cap" in r.stdout
    assert "incomplete" in r.stderr
    assert len(r.stdout) < 60000


def test_long_answer_goes_to_a_temp_file_with_a_preview(h):
    h.configure(answer="y" * 20000)
    r = h.delegate()
    assert r.returncode == 0
    assert "18000 more characters" in r.stdout and len(r.stdout) < 4000
    path = [ln for ln in r.stdout.splitlines() if ln.startswith("full answer written to: ")][0].split(": ", 1)[1]
    assert path.startswith(os.path.realpath(h.tmpdir)) or path.startswith(str(h.tmpdir))
    assert len(open(path).read()) == 20000
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_r8_answer_is_never_written_into_the_repo(h):
    before = sorted(str(p.relative_to(h.repo)) for p in h.repo.rglob("*"))
    h.configure(answer="z" * 20000)
    h.delegate()
    after = sorted(str(p.relative_to(h.repo)) for p in h.repo.rglob("*"))
    assert after == sorted(before + [".agy-bridge", ".agy-bridge/events.jsonl"])
    journal = (h.repo / ".agy-bridge" / "events.jsonl").read_text()
    assert "z" * 100 not in journal
    assert json.loads(journal)["exit_code"] == 0


def test_r8_out_inside_repo_outside_scratch_is_refused(h):
    r = h.delegate("--out", str(h.repo / "src" / "answer.txt"))
    assert r.returncode == 4 and "R8" in r.stderr
    assert not (h.repo / "src" / "answer.txt").exists()


def test_r8_out_in_repo_scratch_folder_is_allowed(h):
    r = h.delegate("--out", str(h.repo / ".agy-bridge" / "answer.txt"))
    assert r.returncode == 0, r.stderr
    assert (h.repo / ".agy-bridge" / "answer.txt").read_text().strip() == "fake answer"


def test_r8_out_in_temp_folder_is_allowed_and_elsewhere_is_not(h):
    assert h.delegate("--out", str(h.tmpdir / "a.txt")).returncode == 0
    r = h.delegate("--out", str(h.root / "elsewhere.txt"))
    assert r.returncode == 4 and not (h.root / "elsewhere.txt").exists()


def test_r8_out_never_overwrites_and_never_follows_a_symlink(h):
    (h.tmpdir / "exists.txt").write_text("keep")
    r = h.delegate("--out", str(h.tmpdir / "exists.txt"))
    assert r.returncode == 1 and "already exists" in r.stderr
    assert not h.record_path.exists()  # refused before agy was run
    assert (h.tmpdir / "exists.txt").read_text() == "keep"
    (h.tmpdir / "target.txt").write_text("keep")
    os.symlink(h.tmpdir / "target.txt", h.tmpdir / "ln.txt")
    assert h.delegate("--out", str(h.tmpdir / "ln.txt")).returncode == 1
    assert (h.tmpdir / "target.txt").read_text() == "keep"


def test_r8_answer_text_is_not_executed(h):
    h.configure(answer="$(touch /tmp/agy_bridge_pwned); `touch /tmp/agy_bridge_pwned2`")
    r = h.delegate()
    assert r.returncode == 0
    assert not os.path.exists("/tmp/agy_bridge_pwned") and not os.path.exists("/tmp/agy_bridge_pwned2")


def test_r8_terminal_escapes_are_stripped_from_the_answer(h):
    h.configure(answer="clean \x1b[31mred\x1b[0m \x07text")
    assert "\x1b" not in h.delegate().stdout


# ---------- R5 / R6 / R1 end to end ----------

def test_r5_secret_file_is_refused_end_to_end_without_echoing_it(h):
    (h.repo / "cfg.py").write_text("password = 'abcdefghij0123456789'\n")
    r = h.run("delegate", "--file", "cfg.py")
    assert r.returncode == 4 and "R5" in r.stderr and "cfg.py" in r.stderr
    assert "abcdefghij" not in r.stdout + r.stderr
    assert not h.record_path.exists()


def test_r5_clean_file_passes(h):
    assert h.delegate().returncode == 0


def test_r6_symlink_is_refused_end_to_end(h, tmp_path):
    (tmp_path / "o.txt").write_text("x")
    os.symlink(tmp_path / "o.txt", h.repo / "ln.txt")
    r = h.run("delegate", "--file", "ln.txt")
    assert r.returncode == 4 and "R6" in r.stderr


def test_r1_env_file_is_refused_and_a_normal_file_is_not(h):
    (h.repo / ".env").write_text("A=1\n")
    r = h.run("delegate", "--file", ".env")
    assert r.returncode == 4 and "R1" in r.stderr
    assert h.delegate().returncode == 0


def test_r1_repo_local_rules_add_and_cannot_remove(h):
    (h.repo / ".agy-bridge.json").write_text('{"deny": ["notes.md"]}')
    assert h.run("delegate", "--file", "notes.md").returncode == 4
    (h.repo / ".agy-bridge.json").write_text('{"allow": [".env"]}')
    assert h.delegate().returncode == 1


# ---------- R7: the log holds metadata only ----------

def test_r7_log_has_metadata_only_and_mode_0600(h):
    (h.repo / "src" / "a.py").write_text("print('UNIQUE_CONTENT_MARKER')\n")
    task = "UNIQUE_TASK_MARKER please"
    r = h.delegate(task=task)
    assert r.returncode == 0
    raw = h.log_path.read_text()
    entry = json.loads(raw.splitlines()[-1])
    assert set(entry) == {"command", "exit", "model", "files", "bytes", "seconds", "task_sha256", "ts"}
    for forbidden in ("UNIQUE_CONTENT_MARKER", "UNIQUE_TASK_MARKER", "a.py", "src"):
        assert forbidden not in raw
    assert stat.S_IMODE(os.stat(h.log_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(h.log_path.parent).st_mode) == 0o700


def test_r7_log_mode_is_repaired_if_it_was_too_open(h):
    h.log_path.parent.mkdir(parents=True)
    h.log_path.write_text("")
    os.chmod(h.log_path, 0o644)
    h.delegate()
    assert stat.S_IMODE(os.stat(h.log_path).st_mode) == 0o600


def test_r7_refusals_are_logged_without_names_or_content(h):
    (h.repo / "cfg.py").write_text("password = 'abcdefghij0123456789'\n")
    h.run("delegate", "--file", "cfg.py")
    raw = h.log_path.read_text()
    assert '"exit": 4' in raw and "cfg.py" not in raw and "abcdefghij" not in raw


def test_r7_nothing_is_printed_from_file_contents_on_success_or_dry_run(h):
    (h.repo / "src" / "a.py").write_text("print('UNIQUE_CONTENT_MARKER')\n")
    for cmd in ("delegate", "dry-run"):
        r = h.run(cmd, "--file", "src/a.py")
        assert "UNIQUE_CONTENT_MARKER" not in r.stdout + r.stderr


def test_r7_the_tests_do_not_touch_the_real_home(h):
    h.delegate()
    assert h.log_path.exists()


# ---------- dry-run ----------

def test_dry_run_shows_manifest_and_argv_and_sends_nothing(h):
    r = h.run("dry-run", "--file", "src/a.py")
    assert r.returncode == 0, r.stderr
    assert "No file contents or task text have been sent" in r.stdout and "src/a.py" in r.stdout and "--print" in r.stdout
    assert not h.record_path.exists() and h.leftovers() == []
    assert not h.log_path.exists()


def test_dry_run_flag_on_delegate_is_the_same(h):
    r = h.run("delegate", "--file", "src/a.py", "--dry-run")
    assert r.returncode == 0 and "No file contents or task text have been sent" in r.stdout


def test_dry_run_applies_the_same_refusals(h):
    (h.repo / ".env").write_text("A=1\n")
    assert h.run("dry-run", "--file", ".env").returncode == 4


def test_dry_run_without_agy_still_shows_the_manifest(h):
    r = h.run("dry-run", "--file", "src/a.py", extra_env={"PATH": "/nonexistent"}, with_fake=False)
    assert r.returncode == 0 and "unavailable" in r.stdout


# ---------- doctor ----------

def test_doctor_ok_offline(h):
    r = h.run("doctor", "--offline")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ok" in r.stdout and "fail" not in r.stdout


def test_doctor_lists_models_online_via_the_fake(h):
    r = h.run("doctor")
    assert r.returncode == 0 and "2 line(s) listed" in r.stdout


def test_doctor_reports_missing_agy(h):
    r = h.run("doctor", "--offline", extra_env={"PATH": "/nonexistent"}, with_fake=False)
    assert r.returncode == 13 and "fail" in r.stdout


def test_doctor_reports_unsupported_version(h):
    h.configure(version="9.1.0")
    assert h.run("doctor", "--offline").returncode == 14


def test_doctor_flags_a_loose_log_mode(h):
    h.log_path.parent.mkdir(parents=True)
    h.log_path.write_text("")
    os.chmod(h.log_path, 0o644)
    r = h.run("doctor", "--offline")
    assert r.returncode != 0 and "log file mode" in r.stdout


# ---------- finding 1 through the real command line ----------

def test_r1_repo_into_a_codex_folder_is_refused_end_to_end(h):
    (h.home / ".codex").mkdir()
    (h.home / ".codex" / "auth.json").write_text('{"tokens": "x"}')
    r = h.raw("delegate", "--repo", str(h.home / ".codex"), "--file", "auth.json", "--", "t")
    assert r.returncode == 4 and "R1" in r.stderr and not h.record_path.exists()


def test_r1_repo_that_is_a_dot_git_folder_is_refused_end_to_end(h):
    (h.repo / ".git" / "config").write_text("[core]\n")
    r = h.raw("delegate", "--repo", str(h.repo / ".git"), "--file", "config", "--", "t")
    assert r.returncode == 4 and not h.record_path.exists()


def test_r6_repo_that_is_home_is_refused_end_to_end_even_with_the_override(h):
    (h.home / ".config" / "gcloud").mkdir(parents=True)
    (h.home / ".config" / "gcloud" / "application_default_credentials.json").write_text("{}")
    for extra in ([], ["--allow-non-git-root"]):
        r = h.raw("delegate", "--repo", str(h.home), "--file", ".config/gcloud/application_default_credentials.json",
                  *extra, "--", "t")
        assert r.returncode == 4 and not h.record_path.exists()


def test_r6_repo_without_dot_git_needs_the_explicit_flag(h):
    os.rmdir(h.repo / ".git")
    r = h.delegate()
    assert r.returncode == 4 and "no .git entry" in r.stderr
    assert h.delegate("--allow-non-git-root").returncode == 0


# ---------- finding 4: PATH ----------

def _plant(h, folder):
    planted = folder / "agy"
    marker = h.root / "PLANTED_RAN"
    planted.write_text("#!/bin/sh\necho ran >> %s\n[ \"$1\" = --version ] && echo 'agy 1.0.0'\necho planted\n" % marker)
    planted.chmod(0o755)
    return marker


def test_r2_empty_path_entry_does_not_run_an_agy_planted_in_the_repo(h):
    marker = _plant(h, h.repo)
    for command in ("delegate", "dry-run"):
        r = h.run(command, "--file", "src/a.py", extra_env={"PATH": "/usr/bin:/bin:"}, with_fake=False, cwd=h.repo)
        assert not marker.exists(), command
    assert r.returncode == 0 and "unavailable" in r.stdout  # dry-run shows nothing was found
    assert h.run("delegate", "--file", "src/a.py", extra_env={"PATH": "/usr/bin:/bin:"}, with_fake=False,
                 cwd=h.repo).returncode == 13


def test_r2_relative_path_entry_is_ignored(h):
    marker = _plant(h, h.repo)
    r = h.run("delegate", "--file", "src/a.py", extra_env={"PATH": "/usr/bin:/bin:."}, with_fake=False, cwd=h.repo)
    assert r.returncode == 13 and not marker.exists()


def test_r2_agy_found_inside_the_repo_through_an_absolute_entry_is_refused(h):
    (h.repo / "tools").mkdir()
    marker = _plant(h, h.repo / "tools")
    r = h.run("delegate", "--file", "src/a.py", extra_env={"PATH": "%s:/usr/bin:/bin" % (h.repo / "tools")},
              with_fake=False)
    assert r.returncode == 13 and "inside the repository" in r.stderr and not marker.exists()


def test_r2_child_path_has_no_empty_or_relative_entries(h):
    r = h.delegate(extra_env={"PATH": "%s::relative/bin:.:/usr/bin" % h.bindir}, with_fake=False)
    assert r.returncode == 0, r.stderr
    entries = h.record()["env"]["PATH"].split(":")
    assert entries == [str(h.bindir), "/usr/bin"]


# ---------- finding 5: what dry-run runs ----------

def test_dry_run_checks_the_version_in_an_empty_folder_not_in_staging(h):
    r = h.run("dry-run", "--file", "src/a.py")
    side = h.side_record("version")
    assert r.returncode == 0
    assert side["listing"] == [] and "agy-bridge-ver-" in os.path.basename(side["cwd"])
    assert not h.record_path.exists()  # no task call


def test_delegate_checks_the_version_in_an_empty_folder_too(h):
    h.delegate()
    assert h.side_record("version")["listing"] == []


# ---------- finding 7: environments of the helper calls ----------

def test_r2_agy_version_call_gets_only_the_five_names(h):
    h.run("dry-run", "--file", "src/a.py", extra_env=SECRETS)
    assert set(h.side_record("version")["env"]) - _ADDED_BY_MACOS_PYTHON == {"HOME", "PATH", "LANG", "TERM", "TMPDIR"}


def test_r2_doctor_models_call_gets_only_the_five_names(h):
    assert h.run("doctor", extra_env=SECRETS).returncode == 0
    assert set(h.side_record("models")["env"]) - _ADDED_BY_MACOS_PYTHON == {"HOME", "PATH", "LANG", "TERM", "TMPDIR"}
    assert set(h.side_record("version")["env"]) - _ADDED_BY_MACOS_PYTHON == {"HOME", "PATH", "LANG", "TERM", "TMPDIR"}


def test_r3_validate_model_rejects_flag_like_and_odd_names():
    from agy_bridge import config
    for bad in ("--dangerously-skip-permissions", "--x", "-m", "a b", "a;b", "", "x" * 80, "$(id)"):
        with pytest.raises(config.UsageError):
            config.validate_model(bad)
    assert config.validate_model("gemini-3.5-pro") == "gemini-3.5-pro" and config.validate_model(None) is None


def test_r3_flag_like_model_values_are_refused_through_the_cli(h):
    for bad in ("--x", "--dangerously-skip-permissions", "--yolo"):
        r = h.delegate("--model=" + bad)
        assert r.returncode == 1 and not h.record_path.exists(), bad


def test_r7_log_file_that_is_a_symlink_is_not_written_through(h):
    h.log_path.parent.mkdir(parents=True)
    target = h.root / "target.txt"
    target.write_text("")
    os.symlink(target, h.log_path)
    r = h.delegate()
    assert r.returncode == 0 and target.read_text() == "" and "could not write the log" in r.stderr


def test_r7_log_folder_that_is_a_symlink_is_not_followed(h):
    elsewhere = h.root / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, h.home / ".agy-bridge")
    assert h.delegate().returncode == 0
    assert os.listdir(elsewhere) == []


def test_r7_open_log_folder_is_tightened_to_0700(h):
    (h.home / ".agy-bridge").mkdir()
    os.chmod(h.home / ".agy-bridge", 0o777)
    h.delegate()
    assert stat.S_IMODE(os.stat(h.home / ".agy-bridge").st_mode) == 0o700


def test_r7_home_override_is_ignored_without_the_test_marker(monkeypatch, tmp_path):
    from agy_bridge import config
    monkeypatch.setenv("AGY_BRIDGE_HOME", str(tmp_path / "x"))
    assert config.bridge_home() == str(tmp_path / "x")  # marker exists while the suite runs
    monkeypatch.setattr(config, "TEST_MARKER", str(tmp_path / "no-such-marker"))
    assert config.bridge_home() == os.path.join(os.path.expanduser("~"), ".agy-bridge")


def test_non_utf8_task_is_a_usage_error_not_a_traceback(h):
    r = subprocess.run([sys.executable.encode(), BIN.encode(), b"delegate", b"--repo", str(h.repo).encode(),
                        b"--file", b"src/a.py", b"--", b"bad \xff task"],
                       env=h.env(), capture_output=True)
    assert r.returncode == 1 and b"Traceback" not in r.stderr and b"UTF-8" in r.stderr


def test_double_sigint_still_cleans_up_without_a_traceback(h):
    h.configure(mode="timeout")
    proc = subprocess.Popen([sys.executable, BIN, "delegate", "--repo", str(h.repo), "--file", "src/a.py", "--",
                             "t"], env=h.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    for _ in range(100):
        if h.record_path.exists():
            break
        time.sleep(0.05)
    proc.send_signal(signal.SIGINT)
    proc.send_signal(signal.SIGINT)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 130 and h.leftovers() == [] and b"Traceback" not in err


def test_stale_staging_folder_is_swept_at_the_start_of_a_delegation(h):
    old = h.tmpdir / "agy-bridge-ab12cd34"
    old.mkdir(mode=0o700)
    past = time.time() - 4 * 3600
    os.utime(old, (past, past))
    h.delegate()
    assert h.leftovers() == []


def test_r8_write_new_file_itself_refuses_to_overwrite_or_follow(tmp_path):
    from agy_bridge import report
    existing = tmp_path / "e.txt"
    existing.write_text("keep")
    with pytest.raises(FileExistsError):
        report.write_new_file(str(existing), "new")
    assert existing.read_text() == "keep"
    os.symlink(existing, tmp_path / "l.txt")
    with pytest.raises(OSError):
        report.write_new_file(str(tmp_path / "l.txt"), "new")
    assert existing.read_text() == "keep"


# ---------- round 2 ----------

def test_r2_relative_path_entry_is_not_used_for_the_lookup_even_outside_the_repo(h):
    elsewhere = h.root / "elsewhere"
    elsewhere.mkdir()
    marker = _plant(h, elsewhere)  # planted agy in the caller's cwd, which is not the repo
    r = h.run("delegate", "--file", "src/a.py", extra_env={"PATH": "/usr/bin:/bin:."}, with_fake=False, cwd=elsewhere)
    assert r.returncode == 13 and not marker.exists()
    r = h.run("delegate", "--file", "src/a.py", extra_env={"PATH": "/usr/bin:/bin:"}, with_fake=False, cwd=elsewhere)
    assert r.returncode == 13 and not marker.exists()


def test_doctor_refuses_an_agy_that_lives_inside_the_repo(h):
    (h.repo / "tools").mkdir()
    marker = _plant(h, h.repo / "tools")
    r = h.run("doctor", "--offline", "--repo", str(h.repo),
              extra_env={"PATH": "%s:/usr/bin:/bin" % (h.repo / "tools")}, with_fake=False)
    assert r.returncode == 13 and "inside the repository" in r.stdout and not marker.exists()


def test_r6_agy_bridge_json_through_a_symlink_is_refused(h):
    (h.root / "real.json").write_text('{"deny": []}')
    os.symlink(h.root / "real.json", h.repo / ".agy-bridge.json")
    r = h.delegate()
    assert r.returncode == 1 and "agy-bridge.json" in r.stderr and not h.record_path.exists()


def test_later_stop_signals_are_ignored_after_the_first():
    code = ("import os, signal\\nfrom agy_bridge import cli\\ncli.install_stop_handlers()\\n"
            "try:\\n    os.kill(os.getpid(), signal.SIGINT)\\nexcept KeyboardInterrupt:\\n    pass\\n"
            "print([signal.getsignal(s) == signal.SIG_IGN for s in cli.STOP_SIGNALS])\\n")
    out = subprocess.run([sys.executable, "-c", code.replace("\\n", "\n")], capture_output=True, text=True,
                         env=dict(os.environ, PYTHONPATH=os.path.join(os.path.dirname(BIN), "..", "src")))
    assert out.stdout.strip() == "[True, True, True]", out.stderr


def test_r7_home_override_needs_the_env_switch_as_well_as_the_marker(monkeypatch, tmp_path):
    from agy_bridge import config
    monkeypatch.setenv("AGY_BRIDGE_HOME", str(tmp_path / "x"))
    assert config.bridge_home() == str(tmp_path / "x")
    monkeypatch.delenv("AGY_BRIDGE_TEST_MODE")
    assert config.bridge_home() == os.path.join(os.path.expanduser("~"), ".agy-bridge")


def test_dry_run_says_exactly_what_ran_and_that_no_git_is_run(h):
    r = h.run("dry-run", "--file", "src/a.py")
    assert "Program run: only 'agy --version'" in r.stdout and "runs no git" in r.stdout


def test_dry_run_without_agy_says_no_program_was_run(h):
    r = h.run("dry-run", "--file", "src/a.py", extra_env={"PATH": "/nonexistent"}, with_fake=False)
    assert r.returncode == 0 and "No program was run" in r.stdout and "Program run:" not in r.stdout


def test_dry_run_with_an_agy_that_cannot_start_says_no_program_was_run(h):
    (h.bindir / "agy").write_text("#!/nonexistent/interpreter\n")  # found on PATH, but exec fails
    r = h.run("dry-run", "--file", "src/a.py")
    assert r.returncode == 0 and "No program was run" in r.stdout


def test_diff_option_no_longer_exists(h):
    r = h.run("delegate", "--file", "src/a.py", "--diff", "HEAD~1..HEAD")
    assert r.returncode == 1 and "unrecognized" in r.stderr and not h.record_path.exists()
