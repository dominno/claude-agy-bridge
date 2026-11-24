import sys

import pytest

from agy_bridge import config


def test_delegate_treats_provider_timeout_with_partial_answer_as_timeout(h):
    provider = h.bindir / 'agy'
    provider.write_text(
        f'#!{sys.executable}\n'
        'import sys\n'
        'if sys.argv[1:] == ["--version"]:\n'
        '    print("1.4.3")\n'
        'else:\n'
        '    print("partial answer")\n'
        '    print("agy: print timeout exceeded", file=sys.stderr)\n',
        encoding='utf-8',
    )
    out = h.tmpdir / 'answer.txt'
    result = h.delegate('--out', str(out))
    assert result.returncode == config.EXIT_TIMEOUT
    assert 'partial answer' in result.stdout
    assert 'time limit' in result.stderr
    assert not out.exists()


def test_delegate_rejects_oversized_timeout_as_invalid_arguments(h):
    result = h.delegate('--timeout', '9' * 5000 + 's')
    assert result.returncode == config.EXIT_USAGE
    assert 'timeout must be between 1 second and 1 hour' in result.stderr
    assert not h.record_path.exists()


def test_job_timeout_rejects_oversized_number_with_job_limit():
    with pytest.raises(config.UsageError, match='job timeout must be between 1 second and 24 hours'):
        config.parse_duration('9' * 5000 + 'h', max_seconds=86400, label='job timeout', ascii_lowercase_only=True)


def test_duration_preserves_leading_zeroes_and_unicode_input():
    assert config.parse_duration('0' * 5000 + '1h') == 3600
    assert config.parse_duration('  ٠١H  ') == 3600
    with pytest.raises(config.UsageError, match='job timeout must look like'):
        config.parse_duration('٠١h', max_seconds=86400, label='job timeout', ascii_lowercase_only=True)
