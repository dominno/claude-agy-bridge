import os
import stat
import subprocess
import tempfile
import time

import pytest

from agy_bridge import stage
from agy_bridge.config import UsageError


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src" / "deep").mkdir(parents=True)
    (root / "src" / "a.py").write_text("print('a')\n")
    (root / "src" / "deep" / "b.py").write_text("print('b')\n")
    (root / "README.md").write_text("hello\n")
    return root


def prep(repo, files=(), globs=(), task="summarise", **kw):
    kw.setdefault("allow_non_git", True)  # the fixture repo has no .git; root checks have their own tests
    return stage.prepare(str(repo), list(files), list(globs), task, **kw)


def refused(repo, **kw):
    with pytest.raises(stage.Refused) as info:
        prep(repo, **kw)
    return info.value.refusals


def test_r6_plain_files_are_accepted_and_hashed(repo):
    _, p = prep(repo, files=["src/a.py", "README.md"])
    assert [e.rel for e in p.entries] == ["src/a.py", "README.md"]
    assert len(p.entries[0].sha256) == 64


def test_r6_symlink_to_file_is_refused(repo, tmp_path):
    (tmp_path / "outside.txt").write_text("x")
    os.symlink(tmp_path / "outside.txt", repo / "link.txt")
    (r,) = refused(repo, files=["link.txt"])
    assert r.rule == "R6" and "symlink" in r.reason


def test_r6_symlink_to_directory_is_refused(repo, tmp_path):
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "f.txt").write_text("x")
    os.symlink(tmp_path / "other", repo / "linkdir")
    (r,) = refused(repo, files=["linkdir/f.txt"])
    assert r.rule == "R6"


def test_r6_dotdot_is_refused(repo):
    (r,) = refused(repo, files=["src/../../etc/hosts"])
    assert r.rule == "R6" and "'..'" in r.reason


def test_r6_absolute_path_outside_repo_is_refused(repo, tmp_path):
    (tmp_path / "o.txt").write_text("x")
    (r,) = refused(repo, files=[str(tmp_path / "o.txt")])
    assert r.rule == "R6" and "outside" in r.reason


def test_r6_absolute_path_inside_repo_is_fine(repo):
    _, p = prep(repo, files=[str(repo / "src" / "a.py")])
    assert p.entries[0].rel == "src/a.py"


def test_r6_fifo_is_refused_without_hanging(repo):
    os.mkfifo(repo / "pipe")
    (r,) = refused(repo, files=["pipe"])
    assert r.rule == "R6" and "regular" in r.reason


def test_r6_real_device_file_is_refused():
    # /dev/null is a real character device, opened directly (no symlink involved).
    with pytest.raises(stage.Refused) as info:
        stage._read_regular("/dev", "null", 1000)
    assert "not a regular file" in info.value.refusals[0].reason


def test_r6_real_fifo_is_refused_by_the_reader(repo):
    os.mkfifo(repo / "pipe")
    with pytest.raises(stage.Refused) as info:
        stage._read_regular(str(repo), "pipe", 1000)
    assert "not a regular file" in info.value.refusals[0].reason


def test_r6_directory_is_refused(repo):
    (r,) = refused(repo, files=["src"])
    assert r.rule == "R6"


def test_r6_file_over_limit_is_refused(repo):
    (repo / "big.txt").write_text("x" * 5000)
    (r,) = refused(repo, files=["big.txt"], max_file_bytes=1000)
    assert r.rule == "R6" and "size" in r.reason


def test_r6_total_over_limit_is_refused(repo):
    rs = refused(repo, files=["src/a.py", "README.md"], max_bytes=10)
    assert any("total" in r.reason for r in rs)


def test_r6_binary_file_is_refused(repo):
    (repo / "blob.bin").write_bytes(b"abc\0def")
    (repo / "latin.txt").write_bytes(b"caf\xe9")
    assert refused(repo, files=["blob.bin"])[0].reason == "binary file"
    assert refused(repo, files=["latin.txt"])[0].reason == "binary file"


def test_r6_missing_file_is_a_usage_error(repo):
    with pytest.raises(UsageError):
        prep(repo, files=["nope.txt"])


def test_r6_reserved_names_are_refused(repo):
    (repo / "TASK.md").write_text("x")
    (r,) = refused(repo, files=["TASK.md"])
    assert r.rule == "R6" and "reserved" in r.reason


def test_r6_glob_escape_is_refused(repo):
    assert refused(repo, globs=["../*"])[0].rule == "R6"
    assert refused(repo, globs=["/etc/*"])[0].rule == "R6"


def test_r6_glob_matches_inside_root_only_and_skips_denied(repo):
    (repo / ".env").write_text("A=1\n")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "x.js").write_text("x\n")
    _, p = prep(repo, globs=["**/*"])
    names = sorted(e.rel for e in p.entries)
    assert names == ["README.md", "src/a.py", "src/deep/b.py"]
    assert p.skipped_by_denylist == 1  # .env; node_modules is never walked


def test_r6_glob_star_levels(repo):
    _, p = prep(repo, globs=["src/*.py"])
    assert [e.rel for e in p.entries] == ["src/a.py"]
    _, p = prep(repo, globs=["src/**/*.py"])
    assert sorted(e.rel for e in p.entries) == ["src/a.py", "src/deep/b.py"]


def test_r6_glob_does_not_follow_a_symlinked_directory(repo, tmp_path):
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "f.txt").write_text("x")
    os.symlink(tmp_path / "other", repo / "linkdir")
    with pytest.raises(UsageError):  # nothing matched: the linked folder was not entered
        prep(repo, globs=["**/*.txt"])


def test_r6_symlink_file_found_by_glob_is_refused(repo, tmp_path):
    (tmp_path / "o.py").write_text("x")
    os.symlink(tmp_path / "o.py", repo / "src" / "link.py")
    assert refused(repo, globs=["src/*.py"])[0].reason == "symlink"


def test_r1_explicit_denylisted_file_is_refused(repo):
    (repo / "src" / ".env").write_text("A=1\n")
    (r,) = refused(repo, files=["src/.env"])
    assert r.rule == "R1"


def test_r1_denylisted_name_in_nested_folder_is_refused(repo):
    (repo / "src" / "deep" / "secrets.yaml").write_text("a: 1\n")
    (r,) = refused(repo, files=["src/deep/secrets.yaml"])
    assert r.rule == "R1"


def test_r1_repo_local_rule_is_applied(repo):
    (repo / "private.txt").write_text("x\n")
    (r,) = refused(repo, files=["private.txt"], extra_deny=["private.txt"])
    assert r.rule == "R1"


def test_r5_secret_in_file_is_refused_with_count_and_no_match(repo):
    (repo / "cfg.py").write_text("a = 1\npassword = 'abcdefghij0123456789'\ntoken: zzzzzzzzzzzzzzzzzzzz\n")
    (r,) = refused(repo, files=["cfg.py"])
    assert r.rule == "R5" and r.count >= 2 and r.where == "cfg.py"
    text = stage.describe(r)
    assert "abcdefghij" not in text and "zzzzzz" not in text and str(r.count) in text


def test_task_file_lists_the_exact_staged_paths(repo):
    """Without a list the real agy hunted for files and then tried paths outside the folder (denied headless)."""
    _, p = prep(repo, files=["src/a.py", "src/deep/b.py"])
    with stage.materialize(p) as st:
        text = open(os.path.join(st.path, "TASK.md")).read()
    assert '- "src/a.py"' in text and '- "src/deep/b.py"' in text
    assert "read exactly these paths" in text


def test_r5_secret_in_task_text_is_refused(repo):
    (r,) = refused(repo, files=["README.md"], task="use ghp_abcdefghijklmnopqrstuv please")
    assert r.rule == "R5" and r.where == "the task text"


def test_staging_preserves_relative_paths_and_mode_and_cleans_up(repo):
    _, p = prep(repo, files=["src/a.py", "src/deep/b.py"])
    with stage.materialize(p) as st:
        assert stat.S_IMODE(os.stat(st.path).st_mode) == 0o700
        assert os.path.isfile(os.path.join(st.path, "src/a.py"))
        assert os.path.isfile(os.path.join(st.path, "src/deep/b.py"))
        assert os.path.isfile(os.path.join(st.path, "TASK.md"))
        assert "may not write files" in open(os.path.join(st.path, "TASK.md")).read()
        assert stat.S_IMODE(os.stat(os.path.join(st.path, "src/a.py")).st_mode) == 0o600
        path = st.path
    assert not os.path.exists(path)


def test_staging_is_cleaned_up_when_the_body_raises(repo):
    _, p = prep(repo, files=["README.md"])
    with pytest.raises(RuntimeError):
        with stage.materialize(p) as st:
            path = st.path
            raise RuntimeError("boom")
    assert not os.path.exists(path)


def test_staged_bytes_are_the_scanned_bytes(repo):
    # The file changes after prepare(); the staged copy is still what was scanned.
    _, p = prep(repo, files=["README.md"])
    (repo / "README.md").write_text("password = 'abcdefghij0123456789'\n")
    with stage.materialize(p) as st:
        assert open(os.path.join(st.path, "README.md")).read() == "hello\n"


def test_nothing_outside_the_manifest_is_staged(repo):
    _, p = prep(repo, files=["README.md"])
    with stage.materialize(p) as st:
        found = sorted(os.listdir(st.path))
    assert found == ["README.md", "TASK.md"]


def test_no_inputs_is_a_usage_error(repo):
    with pytest.raises(UsageError):
        prep(repo)


# ---------- finding 1: the --repo folder itself (R1 / R6) ----------

def root_refusal(path, home, **kw):
    with pytest.raises(stage.Refused) as info:
        stage.prepare(str(path), ["auth.json"], [], "t", **kw)
    return info.value.refusals[0]


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text('{"x": "y"}')
    (home / ".config" / "gcloud").mkdir(parents=True)
    (home / ".config" / "gcloud" / "application_default_credentials.json").write_text("{}")
    monkeypatch.setenv("HOME", str(home))
    return home


def test_r1_repo_pointing_into_codex_folder_is_refused(fake_home):
    r = root_refusal(fake_home / ".codex", fake_home, allow_non_git=True)
    assert r.rule == "R1"


def test_r1_repo_pointing_at_a_dot_git_folder_is_refused(repo):
    (repo / ".git").mkdir()
    (repo / ".git" / "config").write_text("[core]\n")
    r = root_refusal(repo / ".git", None, allow_non_git=True)
    assert r.rule == "R1"


def test_r6_repo_that_is_the_home_folder_is_refused_even_with_the_override(fake_home):
    r = root_refusal(fake_home, fake_home, allow_non_git=True)
    assert r.rule == "R6" and "home" in r.reason


def test_r6_repo_above_home_and_filesystem_root_are_refused(fake_home):
    assert root_refusal(fake_home.parent, fake_home, allow_non_git=True).rule == "R6"
    assert root_refusal("/", fake_home, allow_non_git=True).rule == "R6"


def test_r6_repo_without_dot_git_is_refused_by_default_and_allowed_with_the_flag(repo):
    r = root_refusal(repo, None)
    assert r.rule == "R6" and "no .git entry" in r.reason
    (repo / ".git").mkdir()
    stage.check_root(str(repo))  # a .git folder at the top level is accepted


def test_r6_override_flag_does_not_unlock_a_denylisted_root(fake_home):
    assert root_refusal(fake_home / ".config" / "gcloud", fake_home, allow_non_git=True).rule == "R1"


def test_r6_gcloud_credentials_via_home_as_repo_are_refused(fake_home):
    with pytest.raises(stage.Refused):
        stage.prepare(str(fake_home), [".config/gcloud/application_default_credentials.json"], [], "t",
                      allow_non_git=True)


# ---------- finding 3: hard links ----------

def test_r6_hard_link_is_refused(repo, tmp_path):
    (tmp_path / "outside.txt").write_text("not a key shape\n")
    os.link(tmp_path / "outside.txt", repo / "linked.txt")
    (r,) = refused(repo, files=["linked.txt"])
    assert r.rule == "R6" and "hard link" in r.reason


def test_r6_single_link_file_is_fine(repo):
    assert prep(repo, files=["README.md"])[1].entries


# ---------- finding 7: the final open must not follow a link that appears after the check ----------

def test_r6_file_swapped_for_a_symlink_after_the_check_is_not_followed(repo, tmp_path, monkeypatch):
    secret = tmp_path / "secret.txt"
    secret.write_text("SHAPELESS-SECRET\n")
    real_stat = os.stat
    swapped = []

    def swapping_stat(path, *a, **kw):
        result = real_stat(path, *a, **kw)
        if path == "a.py" and kw.get("dir_fd") is not None and not swapped:
            swapped.append(1)
            os.remove(repo / "src" / "a.py")
            os.symlink(secret, repo / "src" / "a.py")
        return result

    monkeypatch.setattr(stage.os, "stat", swapping_stat)
    with pytest.raises(stage.Refused):
        stage._read_regular(str(repo), "src/a.py", 1000)
    assert swapped


# ---------- nice items ----------

def test_case_variants_of_reserved_names_are_refused(repo):
    (repo / "task.md").write_text("x\n")
    (r,) = refused(repo, files=["task.md"])
    assert "reserved" in r.reason


def test_two_names_differing_only_in_case_are_refused(repo):
    (r,) = refused(repo, files=["src/a.py", "SRC/A.PY"])
    assert "letter case" in r.reason


def test_same_name_twice_is_deduplicated(repo):
    _, p = prep(repo, files=["src/a.py", "src/a.py"])
    assert len(p.entries) == 1


def test_non_utf8_task_is_a_usage_error_not_a_traceback(repo):
    with pytest.raises(UsageError):
        prep(repo, files=["README.md"], task="bad \udcff task")


def _stale(path, mode=0o700, age=4 * 3600):
    path.mkdir()
    (path / "f").write_text("x")
    os.chmod(path, mode)
    past = time.time() - age
    os.utime(path, (past, past), follow_symlinks=False)


def test_old_staging_folders_are_swept_and_fresh_ones_are_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(stage.tempfile, "gettempdir", lambda: str(tmp_path))
    old, fresh, other = tmp_path / "agy-bridge-ab12cd34", tmp_path / "agy-bridge-ef56gh78", tmp_path / "unrelated"
    ver = tmp_path / "agy-bridge-ver-ab12cd34"
    _stale(old)
    _stale(ver)
    _stale(fresh, age=10)
    _stale(other)
    assert stage.sweep_old_staging() == 2
    assert not old.exists() and not ver.exists() and fresh.exists() and other.exists()


def test_sweep_keeps_folders_of_other_tools_with_a_similar_name_or_other_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(stage.tempfile, "gettempdir", lambda: str(tmp_path))
    look_alike = tmp_path / "agy-bridge-cache-of-other-tool"
    open_mode = tmp_path / "agy-bridge-ab12cd34"
    _stale(look_alike)
    _stale(open_mode, mode=0o755)
    assert stage.sweep_old_staging() == 0
    assert look_alike.exists() and open_mode.exists()


def test_sweep_requires_ownership(tmp_path, monkeypatch):
    monkeypatch.setattr(stage.tempfile, "gettempdir", lambda: str(tmp_path))
    mine = tmp_path / "agy-bridge-ab12cd34"
    _stale(mine)
    monkeypatch.setattr(stage.os, "getuid", lambda: os.stat(mine).st_uid + 1)
    assert stage.sweep_old_staging() == 0 and mine.exists()


def test_sweep_never_follows_a_symlink(tmp_path, monkeypatch):
    monkeypatch.setattr(stage.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    target = tmp_path / "target"
    (target / "x").mkdir(parents=True)
    os.chmod(target, 0o700)
    link = tmp_path / "tmp" / "agy-bridge-ab12cd34"
    os.symlink(target, link)
    past = time.time() - 4 * 3600
    os.utime(link, (past, past), follow_symlinks=False)
    os.utime(target, (past, past))
    assert stage.sweep_old_staging() == 0
    assert (target / "x").exists() and os.path.islink(link)


def test_cleanup_does_not_chmod_what_a_symlink_points_at(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("v")
    os.chmod(victim, 0o400)
    d = tmp_path / "agy-bridge-stale"
    (d / "ro").mkdir(parents=True)
    os.symlink(victim, d / "ro" / "lnk")
    os.chmod(d / "ro", 0o500)  # removing lnk fails until the handler fixes the folder
    stage._cleanup(str(d))
    assert not d.exists()
    assert stat.S_IMODE(os.stat(victim).st_mode) == 0o400 and victim.exists()


def test_cleanup_blocks_signals_while_removing(tmp_path, monkeypatch):
    import signal
    seen = []
    real = signal.pthread_sigmask

    def spy(how, mask):
        seen.append((how, set(mask)))
        return real(how, mask)

    monkeypatch.setattr(stage.signal, "pthread_sigmask", spy)
    d = tmp_path / "x"
    d.mkdir()
    stage._cleanup(str(d))
    assert seen[0][0] == signal.SIG_BLOCK and signal.SIGINT in seen[0][1]
    assert not d.exists()


# ---------- round 2, finding 1: root compared by inode, canonical case, hidden folders, real .git ----------

def _case_insensitive(path):
    swapped = str(path).upper()
    return swapped != str(path) and os.path.exists(swapped) and os.path.samefile(swapped, path)


def test_r6_home_in_other_letter_case_is_still_home(fake_home):
    if not _case_insensitive(fake_home):
        pytest.skip("needs a case-insensitive filesystem")
    assert root_refusal(str(fake_home).upper(), fake_home, allow_non_git=True).rule == "R6"
    assert root_refusal(str(fake_home.parent).upper(), fake_home, allow_non_git=True).rule == "R6"


def test_r1_denylisted_root_in_other_letter_case_is_still_denied(fake_home):
    if not _case_insensitive(fake_home):
        pytest.skip("needs a case-insensitive filesystem")
    assert root_refusal(str(fake_home / ".codex").upper(), fake_home, allow_non_git=True).rule in ("R1", "R6")


def test_r6_system_folder_is_refused_even_in_other_letter_case_and_with_the_flag(fake_home):
    assert root_refusal("/etc", fake_home, allow_non_git=True).rule == "R6"
    if os.path.exists("/PRIVATE/ETC"):
        assert root_refusal("/PRIVATE/ETC", fake_home, allow_non_git=True).rule == "R6"


def test_r6_canonical_path_uses_the_on_disk_case(fake_home):
    if not _case_insensitive(fake_home):
        pytest.skip("needs a case-insensitive filesystem")
    assert stage.canonical_path(str(fake_home).upper()) == stage.canonical_path(str(fake_home))


def test_r1_symlink_alias_of_home_or_a_secret_folder_is_refused(fake_home, tmp_path):
    os.symlink(fake_home, tmp_path / "alias_home")
    os.symlink(fake_home / ".codex", tmp_path / "innocent")
    assert root_refusal(tmp_path / "alias_home", fake_home, allow_non_git=True).rule == "R6"
    assert root_refusal(tmp_path / "innocent", fake_home, allow_non_git=True).rule == "R1"


def test_r1_folder_inside_a_hidden_home_folder_is_refused(fake_home):
    foo = fake_home / ".config" / "foo"
    (foo / ".git").mkdir(parents=True)
    r = root_refusal(foo, fake_home)
    assert r.rule == "R1" and "hidden" in r.reason
    other = fake_home / ".local" / "share" / "proj"
    (other / ".git").mkdir(parents=True)
    assert root_refusal(other, fake_home, allow_non_git=True).rule == "R1"


def test_r1_ordinary_project_under_home_is_accepted(fake_home):
    proj = fake_home / "projects" / "x"
    (proj / ".git").mkdir(parents=True)
    (proj / "a.py").write_text("x = 1\n")
    stage.check_root(str(proj))


def test_r6_dot_git_symlink_or_stray_file_does_not_count(tmp_path):
    for name, make in (("sym", lambda p: os.symlink("/etc", p)),
                       ("file", lambda p: p.write_text("hello\n")),
                       ("far", lambda p: p.write_text("gitdir: /nonexistent\n")),
                       ("outside", lambda p: p.write_text("gitdir: ../elsewhere\n"))):
        root = tmp_path / name
        root.mkdir()
        (tmp_path / "elsewhere").mkdir(exist_ok=True)
        make(root / ".git")
        with pytest.raises(stage.Refused) as info:
            stage.check_root(str(root))
        assert "no .git entry" in info.value.refusals[0].reason, name


def test_r6_dot_git_file_naming_a_real_folder_inside_the_repo_counts(tmp_path):
    root = tmp_path / "wt"
    (root / ".realgit").mkdir(parents=True)
    (root / ".git").write_text("gitdir: .realgit\n")
    stage.check_root(str(root))


def test_r6_filesystem_root_is_refused_on_its_own_even_when_home_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "does-not-exist"))
    assert root_refusal("/", None, allow_non_git=True).reason.endswith("filesystem root")


def check_refusal(path):
    with pytest.raises(stage.Refused) as info:
        stage.check_root(str(path), allow_non_git=True)
    return info.value.refusals[0]


def test_r6_home_is_compared_by_inode_even_when_the_path_text_differs(fake_home, monkeypatch):
    monkeypatch.setattr(stage, "canonical_path", lambda p: str(p))  # no normalising at all
    (fake_home / "projects").mkdir()
    for spelling in (str(fake_home / "projects" / ".."), str(fake_home) + "//", str(fake_home.parent / "." / "home")):
        assert check_refusal(spelling).rule == "R6", spelling
    above = str(fake_home / ".." / ".." / fake_home.parent.name)
    assert check_refusal(above).rule == "R6"


def test_r1_symlink_alias_is_resolved_even_without_the_macos_case_lookup(fake_home, tmp_path, monkeypatch):
    import fcntl
    monkeypatch.delattr(fcntl, "F_GETPATH", raising=False)
    os.symlink(fake_home / ".codex", tmp_path / "plain_name")
    assert check_refusal(tmp_path / "plain_name").rule == "R1"


def test_r6_parent_folder_swapped_for_a_symlink_after_the_check_is_not_followed(repo, tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.py").write_text("SHAPELESS-SECRET\n")
    real_stat = os.stat
    swapped = []

    def swapping_stat(path, *a, **kw):
        result = real_stat(path, *a, **kw)
        if path == "src" and kw.get("dir_fd") is not None and not swapped:
            swapped.append(1)
            import shutil
            shutil.rmtree(repo / "src")
            os.symlink(outside, repo / "src")
        return result

    monkeypatch.setattr(stage.os, "stat", swapping_stat)
    with pytest.raises(stage.Refused):
        stage._read_regular(str(repo), "src/a.py", 1000)
    assert swapped


# ---------- round 3: sweep age, vanished folder, system folders, password-database home ----------

def test_sweep_age_is_longer_than_any_allowed_run_plus_a_margin():
    from agy_bridge import config
    assert config.SWEEP_MIN_AGE_S >= config.MAX_TIMEOUT_S + 3600
    assert config.parse_duration("1h") == config.MAX_TIMEOUT_S
    with pytest.raises(UsageError):
        config.parse_duration("2h")


def test_sweep_keeps_a_folder_as_old_as_the_longest_possible_run(tmp_path, monkeypatch):
    from agy_bridge import config
    monkeypatch.setattr(stage.tempfile, "gettempdir", lambda: str(tmp_path))
    live = tmp_path / "agy-bridge-ab12cd34"
    stale = tmp_path / "agy-bridge-ef56gh78"
    _stale(live, age=config.MAX_TIMEOUT_S + 60)
    _stale(stale, age=config.SWEEP_MIN_AGE_S + 60)
    assert stage.sweep_old_staging() == 1
    assert live.exists() and not stale.exists()


def test_sweep_name_must_have_exactly_the_mkdtemp_length(tmp_path, monkeypatch):
    monkeypatch.setattr(stage.tempfile, "gettempdir", lambda: str(tmp_path))
    longer = tmp_path / "agy-bridge-ab12cd345"
    shorter = tmp_path / "agy-bridge-ab12cd3"
    _stale(longer)
    _stale(shorter)
    assert stage.sweep_old_staging() == 0 and longer.exists() and shorter.exists()


def test_cleanup_of_a_folder_that_vanished_does_not_chmod_its_parent(tmp_path):
    parent = tmp_path / "tmpdir"
    parent.mkdir()
    os.chmod(parent, 0o755)
    stage._cleanup(str(parent / "agy-bridge-gone"))
    assert stat.S_IMODE(os.stat(parent).st_mode) == 0o755


def test_same_matches_different_spellings_of_one_folder(tmp_path):
    (tmp_path / "a").mkdir()
    assert stage._same(str(tmp_path / "a" / ".." / "a"), str(tmp_path / "a"))
    if _case_insensitive(tmp_path):  # a string comparison of resolved paths would say "different"
        assert stage._same(str(tmp_path / "a"), str(tmp_path / "A"))
    assert not stage._same(str(tmp_path / "a"), str(tmp_path / "missing"))


@pytest.mark.parametrize("folder", [
    "/Library", "/usr", "/usr/local", "/dev", "/var", "/var/log", "/Volumes", "/etc", "/System", "/Users", "/sbin",
    "/bin", "/private", "/private/etc", "/private/var/log", "/System/Volumes/Data/private"])
def test_r6_system_folders_are_refused_even_with_the_flag(folder):
    if not os.path.isdir(folder):
        pytest.skip("not on this machine")
    with pytest.raises(stage.Refused) as info:
        stage.check_root(folder, allow_non_git=True)
    assert info.value.refusals[0].rule == "R6"


def test_r6_children_of_exact_only_folders_are_not_blocked_by_that_rule(tmp_path):
    # /var and /Volumes themselves are refused, but a project below /var/folders (the macOS temp dir) is fine.
    (tmp_path / ".git").mkdir()
    stage.check_root(str(tmp_path))


def test_r1_library_inside_home_is_refused_with_the_flag(fake_home):
    (fake_home / "Library" / "Keychains").mkdir(parents=True)
    with pytest.raises(stage.Refused) as info:
        stage.check_root(str(fake_home / "Library" / "Keychains"), allow_non_git=True)
    assert info.value.refusals[0].rule == "R1"


def test_r6_home_from_the_password_database_is_used_even_if_home_env_lies(tmp_path, monkeypatch):
    import pwd
    real_home = tmp_path / "realhome"
    (real_home / "proj").mkdir(parents=True)
    liar = tmp_path / "liar"
    liar.mkdir()
    monkeypatch.setenv("HOME", str(liar))
    entry = pwd.getpwuid(os.getuid())
    fake = pwd.struct_passwd((entry.pw_name, entry.pw_passwd, entry.pw_uid, entry.pw_gid, entry.pw_gecos,
                              str(real_home), entry.pw_shell))
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: fake)
    with pytest.raises(stage.Refused) as info:
        stage.check_root(str(real_home), allow_non_git=True)
    assert "home folder" in info.value.refusals[0].reason
    (real_home / ".config" / "x").mkdir(parents=True)
    with pytest.raises(stage.Refused):
        stage.check_root(str(real_home / ".config" / "x"), allow_non_git=True)


def test_write_new_refuses_an_existing_symlink_with_o_excl_alone(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    base = tmp_path / "staging"
    base.mkdir()
    os.symlink(victim, base / "f.txt")
    with pytest.raises(FileExistsError):
        stage._write_new(str(base), "f.txt", b"new")
    assert victim.read_text() == "keep"


def test_r6_scratch_places_inside_system_trees_still_work():
    import shutil
    for base in ("/tmp", "/var/tmp", "/private/tmp"):
        if not os.path.isdir(base):
            continue
        d = tempfile.mkdtemp(dir=base)
        try:
            os.mkdir(os.path.join(d, ".git"))
            stage.check_root(d)
        finally:
            shutil.rmtree(d)
    stage.check_root(tempfile.mkdtemp(), allow_non_git=True)  # the platform temp dir (may be under /var/folders)


def test_r6_the_scratch_folders_themselves_are_not_projects():
    for base in ("/tmp", "/private/tmp", "/var/tmp"):
        if os.path.isdir(base):
            with pytest.raises(stage.Refused):
                stage.check_root(base, allow_non_git=True)


@pytest.fixture
def three_homes(tmp_path, monkeypatch):
    """$HOME, ~ (expanduser) and the password database each name a different folder."""
    import pwd
    homes = {}
    for name in ("env_home", "tilde_home", "pwd_home"):
        (tmp_path / name / "proj").mkdir(parents=True)
        homes[name] = tmp_path / name
    monkeypatch.setenv("HOME", str(homes["env_home"]))
    monkeypatch.setattr(stage.os.path, "expanduser", lambda p: str(homes["tilde_home"]))
    entry = pwd.getpwuid(os.getuid())
    fake = pwd.struct_passwd((entry.pw_name, entry.pw_passwd, entry.pw_uid, entry.pw_gid, entry.pw_gecos,
                              str(homes["pwd_home"]), entry.pw_shell))
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: fake)
    return homes


@pytest.mark.parametrize("which", ["env_home", "tilde_home", "pwd_home"])
def test_r6_every_source_of_the_home_folder_is_used(three_homes, which):
    with pytest.raises(stage.Refused) as info:
        stage.check_root(str(three_homes[which]), allow_non_git=True)
    assert "home folder" in info.value.refusals[0].reason


@pytest.mark.parametrize("which", ["env_home", "tilde_home", "pwd_home"])
def test_r1_hidden_folders_are_refused_below_every_source_of_home(three_homes, which):
    hidden = three_homes[which] / ".cache" / "x"
    hidden.mkdir(parents=True)
    with pytest.raises(stage.Refused):
        stage.check_root(str(hidden), allow_non_git=True)


def test_r5_the_task_length_is_checked_before_the_task_is_scanned(repo, monkeypatch):
    scanned = []
    monkeypatch.setattr(stage, "scan_bytes", lambda b: scanned.append(len(b)) or {})
    with pytest.raises(UsageError):
        prep(repo, files=["README.md"], task="x" * (stage.config.MAX_TASK_CHARS + 1))
    assert scanned == []
