import pytest

from agy_bridge.scan import SECRET_RULES, scan_bytes


@pytest.mark.parametrize("rule", SECRET_RULES, ids=lambda r: r.id)
def test_r5_every_rule_matches_its_positive_example(rule):
    assert rule.id in scan_bytes(rule.positive.encode())


@pytest.mark.parametrize("rule", SECRET_RULES, ids=lambda r: r.id)
def test_r5_no_rule_matches_any_negative_example(rule):
    assert scan_bytes(rule.negative.encode()) == {}


def test_r5_count_is_per_rule_and_never_returns_the_match():
    buf = b"password = 'abcdefghij0123456789'\nsecret: zzzzzzzzzzzzzzzzzzzz\n"
    hits = scan_bytes(buf)
    assert hits["assigned-secret"] == 2
    assert all(isinstance(v, int) for v in hits.values())


def test_r5_very_long_lines_stay_fast_and_clean():
    import time
    from agy_bridge import config
    # The total-size gate refuses anything larger before the scan, so MAX_TOTAL_BYTES is the real worst case.
    start = time.time()
    assert scan_bytes(b"a" * config.MAX_TOTAL_BYTES) == {}
    assert scan_bytes(b"secret " + b" " * 200_000 + b"x") == {}
    assert time.time() - start < 3


def test_r5_ordinary_code_is_clean():
    assert scan_bytes(b"def add(a, b):\n    return a + b\n") == {}


F = "Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4z"  # an invented value


@pytest.mark.parametrize("text", [
    '{"api_key": "%s"}' % F,
    '{"client_secret": "%s"}' % F,
    '{"refresh_token": "1//0g%s"}' % F,
    "SECRET_KEY = '%s'" % F,
    "headers['x-api-key'] = '%s'" % F,
    "OPENAI = 'sk-proj-%s_%s'" % (F[:10], F),
    "k = 'sk-ant-api03-%s-%s'" % (F, F),
    "const k = 'sk_%s'" % ("0123456789abcdef" * 3),
    "key = 'AIza%s'" % F,
    "github_pat_11ABCDEFG0%s" % F,
    "gho_%s" % F,
    "ghs_%s" % F,
    "postgres://admin:%s@db.example/prod" % F,
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.%s" % F,
    "Authorization: Bearer %s" % F,
    "-----BEGIN PGP PRIVATE KEY BLOCK-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "-----BEGIN PRIVATE KEY-----",
    "password = 'ab+cd/ef+gh/ij+kl/mn=='",
    "service_api_key=%s" % F,
])
def test_r5_reviewer_shapes_are_caught(text):
    assert scan_bytes(text.encode())


@pytest.mark.parametrize("text", [
    "risk-averse-behaviour-in-markets-and-more",
    "task_0123456789abcdef0123456789abcdef0123",
    "https://example.com:8080/path/to/file",
    "http://user@host.example/x",
    "-----BEGIN PGP PUBLIC KEY BLOCK-----",
    '{"api_key": ""}',
    "token = short",
    "def password(self): return self._pw",
    "Bearer token in the header",
    "eyJ.a.b",
])
def test_r5_lookalikes_are_not_flagged(text):
    assert scan_bytes(text.encode()) == {}


def test_r5_many_repeated_keywords_stay_fast():
    import time
    # The size gate refuses anything over 200 KB before the scan runs, so 200 KB is the real worst case. The bound is
    # generous (the measured time is about 0.1 s) so that a loaded machine cannot make this flaky.
    start = time.time()
    scan_bytes(b"secret" * 33000)
    scan_bytes(b"token " * 33000)
    assert time.time() - start < 5


@pytest.mark.parametrize("text", [
    "redis://:fakepassword123@cache:6379",
    "authorization: bearer %s" % F,
    'password = "Tr0ub4dor&3!horse"',
    "SECRET_KEY = 'x$k!9@#(fakefakefake)'",
    '{"token": "ya29.%s"}' % F,
    '{"password": "%s"}' % F,
    "hf_" + "a" * 34,
    "https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX",
    "//registry.npmjs.org/:_authToken=npm_" + "a" * 36,
])
def test_r5_round_two_shapes_are_caught(text):
    assert scan_bytes(text.encode())


@pytest.mark.parametrize("text", [
    'password = "${DB_PASSWORD}"',
    'token = "two words here"',
    '{"token": "short"}',
    "shelf_" + "a" * 34,
    "hooks.slack.com/services/docs",
    "redis://cache:6379/0",
])
def test_r5_round_two_lookalikes_are_not_flagged(text):
    assert scan_bytes(text.encode()) == {}


def test_r5_json_rule_matters_on_its_own_for_tokens_with_dots():
    assert "assigned-secret-json" in scan_bytes(('{"token": "ya29.%s"}' % F).encode())


# ---------- round 3: more shapes, no false positive on tokenizer names ----------

@pytest.mark.parametrize("text", [
    "glpat-" + "a" * 24,
    "GOCSPX-" + F[:28],
    "ASIAABCDEFGHIJKLMNOP",
    "SG." + F[:22] + "." + F + F[:11],
    "Authorization: ya29." + F,
    "ya29.a0AfH6SMBxxxxxxxxxxxxxxxxxxxx",
    "password: Tr0ub4dor&3horse!!",
    "db_password = Tr0ub4dor&3horse!!",
    "machine example.com login bob password s3cret",
    "machine example.com\n  login bob\n  password s3cret",
    "Authorization: Basic dXNlcjpwYXNzd29yZA==",
    "authorization = 'basic dXNlcjpwYXNzd29yZA=='",
])
def test_r5_round_three_shapes_are_caught(text):
    assert scan_bytes(text.encode()), text


@pytest.mark.parametrize("text", [
    'tokenizer_name = "bert-base-uncased-model"',
    "tokenizer = AutoTokenizer.from_pretrained('x')",
    'max_tokens = "1000000000000000000"',
    "password: Optional[str]",
    "token_count = 1234567890",
    "basic information about authorization",
    "machine learning models and login pages",
    "ASIAN markets and ASIA-Pacific",
    "glpat-short",
    "SG.short.short",
])
def test_r5_round_three_lookalikes_are_not_flagged(text):
    assert scan_bytes(text.encode()) == {}, text


# ---------- round 3: every pattern is bounded and linear ----------

def _adversarial_inputs(rule, size=200 * 1024):
    pos, neg = rule.positive, rule.negative
    pieces = [pos, neg, pos[:-1], pos[: max(1, len(pos) // 2)], pos[:3], "eyJ", "PRIVATE KEY ", "-----BEGIN ",
              "secret ", "password=", "Bearer ", "a" * 50, "a://", "machine x login y "]
    inputs = [(p * (size // max(1, len(p)) + 1))[:size] for p in pieces]
    half = pos[: max(1, len(pos) // 2)]
    inputs.append(half + "a" * size)
    inputs.append(half + " " * size + "x")
    inputs.append(half + "=" + " " * size)
    return [i.encode("utf-8", "ignore") for i in inputs]


def test_r5_every_pattern_finishes_fast_on_adversarial_200kb_inputs():
    import time
    from agy_bridge.scan import SECRET_RULES, rule_regexes
    by_id = {r.id: r for r in SECRET_RULES}
    for rule_id, regex in rule_regexes():
        for data in _adversarial_inputs(by_id[rule_id]):
            start = time.time()
            list(regex.finditer(data))
            assert time.time() - start < 2, (rule_id, data[:30])


def test_r5_the_two_reported_quadratic_cases_are_fast():
    import time
    for data in (b"eyJ" * 70000, b"PRIVATE KEY " * 17000, b"-----BEGIN " + b"PRIVATE KEY " * 17000,
                 b"eyJaaaaaaaa." * 17000):
        start = time.time()
        scan_bytes(data)
        assert time.time() - start < 2


def test_r5_no_pattern_has_an_unbounded_or_repeated_group():
    import re as _re
    from agy_bridge.scan import SECRET_RULES
    for rule in SECRET_RULES:
        bare = _re.sub(r"\[(?:\\.|[^\]\\])*\]", "[]", rule.pattern)  # drop character classes
        bare = bare.replace("\\\\", "")
        assert ",}" not in bare, rule.id + ": open-ended {n,}"
        assert not _re.search(r"(?<!\\)[*+]", bare), rule.id + ": unbounded * or +"
        assert not _re.search(r"\)[{*+]\d*,?\d*\}?", bare.replace("){0,4}", "").replace("){0,1}", "")
                              .replace(")?", "")), rule.id + ": repeated group"


def test_r5_oversize_inputs_are_refused_before_any_scan(tmp_path, monkeypatch):
    from agy_bridge import stage
    (tmp_path / ".git").mkdir()
    (tmp_path / "big.txt").write_text("x" * 5000)
    (tmp_path / "ok.txt").write_text("y" * 10)
    scanned = []
    monkeypatch.setattr(stage, "scan_bytes", lambda b: scanned.append(len(b)) or {})
    with pytest.raises(stage.Refused):  # one file over the per-file limit
        stage.prepare(str(tmp_path), ["big.txt"], [], "t", max_file_bytes=1000)
    assert 5000 not in scanned
    with pytest.raises(stage.Refused):  # the total goes over the limit at big.txt, so it is not scanned
        stage.prepare(str(tmp_path), ["ok.txt", "big.txt"], [], "t", max_bytes=15)
    assert 5000 not in scanned and 10 in scanned


# ---------- council round 1: more provider tokens and short passwords ----------

@pytest.mark.parametrize("text", [
    "PuTTY-User-Key-File-2: ssh-rsa",
    "DefaultEndpointsProtocol=https;AccountName=x;AccountKey=%s==;" % F,
    "VAULT_ADDR=x hvs.%s" % F,
    "dop_v1_" + "0123456789abcdef" * 4,
    "pypi-AgE" + F + F,
    "shpss_" + "0123456789abcdef" * 2,
    "lin_api_" + F + "abcdefgh",
    "gsk_" + F + F[:10],
    "xai-" + F + F[:10],
    "DB_PASSWORD=hunter22",
    "MYSQL_PWD: s3cretpw",
    "SMTP_PASS='abc12345'",
    "define('DB_PASSWORD', 'wp12pass');",
    'define( "DB_PASSWORD" , "wp12pass" );',
    "export ADMIN_PASSWD=letme1nnow",
    "DB_PASSWORD=hunter22 # dev only",
    "password=hunter22;",
    'password = "old_pass_2"',
    '{"db_password": "s3cretpw"}',
])
def test_r5_council_round_one_shapes_are_caught(text):
    assert scan_bytes(text.encode()), text


@pytest.mark.parametrize("text", [
    "DB_PASSWORD=changeme",
    "DB_PASSWORD=${DB_PASSWORD}",
    "DB_PASSWORD=<your-password-1>",
    "password = get_password(user1)",
    "pwd = os.getcwd()",
    "if password == 'x':",
    "DB_PASS=short1",
    "bypass=abc12345",
    "define('DB_NAME', 'wordpress1');",
    "AccountKey=<your key>",
    "PuTTY is an SSH client",
    "self.password = hashlib.sha256(raw).hexdigest()",
    "password = argon2.hash(raw)",
    "new_password = old_password2",
    "user_pwd=input_field_2",
    "password = config.db_password1",
    "self.pwd = request.form.get2",
])
def test_r5_council_round_one_lookalikes_are_not_flagged(text):
    assert scan_bytes(text.encode()) == {}, text


def test_r5_short_password_rule_stays_fast_on_long_names_and_values():
    import time
    start = time.time()
    scan_bytes(b"A" * 200_000 + b"_PASSWORD=" + b"1" * 200_000)
    scan_bytes(b"_PASS=" * 33000)
    scan_bytes(b"password='" * 20000)
    assert time.time() - start < 3


# ---------- council round 3: *_KEY assignments and more token shapes ----------

@pytest.mark.parametrize("text", [
    'SIGNING_KEY = "9f8e7d6c5b4a39281706f5e4d3c2b1a0"',
    "DB_ENCRYPTION_KEY=Zm9vYmFyYmF6cXV4MTIzNDU2",
    "export MASTER_KEY=a1b2c3d4e5f6a7b8c9d0",
    "const privateKey = 'Mk9xQ2pLr7Vt5wYz3BnA';",
    "signingKey: x9Y8w7V6u5T4s3R2q1P0",
    '{"encryptionKey": "AbCdEf0123456789XyZ="}',
    "session-key: 0123456789abcdefABCD",
    ":app_key => \"base64:Qm9vbVNlY3JldDEyMzQ1Ng==\"",
    "JWT_KEY='hs256:k1e2y3s4e5c6r7e8t9'",
    "hmac_key = 6a2f9c1e7b3d5a8f0c4e",
    "ACCESS_KEY: AbcdEfgh1234Ijkl5678",
    "client_key=c0ffee12deadbeef3456",
    "Endpoint=sb://ns.servicebus.windows.net/;SharedAccessKeyName=root;SharedAccessKey=" + "Ab1+" * 8 + "=",
    "AGE-SECRET-KEY-1" + "QQ9" * 19 + "Q",
    "ATATT3xFfGF0" + "aB3_-" * 8,
    "token: dapi" + "0123456789abcdef" * 2,
])
def test_r5_council_round_three_shapes_are_caught(text):
    assert scan_bytes(text.encode()), text


@pytest.mark.parametrize("text", [
    'KEY_NAME = "primary"',
    "public_key: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexample",
    "signing_key = 'short1'",
    "privateKey = 'abcdefghijklmnopqrstuv'",
    "access_key: ABCDEFGHIJKLMNOPQRSTU",
    "session_key = request.session.session_key",
    "master_key_path = '/etc/keys/master1234567890.pem'",
    "monkey = 1234567890abcdef1234",
    "SharedAccessKeyName=RootManageSharedAccessKey",
    "AGE-SECRET-KEY-1SHORT",
    "rapid" + "0123456789abcdef" * 2,
])
def test_r5_council_round_three_lookalikes_are_not_flagged(text):
    assert scan_bytes(text.encode()) == {}, text


def test_r5_named_key_rule_stays_fast_on_long_runs():
    import time
    start = time.time()
    scan_bytes(b"private_key=" * 20000)
    scan_bytes(b"signing_key = '" + b"a" * 400_000)
    scan_bytes(b"crypt_key:" + b"1" * 400_000)
    assert time.time() - start < 3
