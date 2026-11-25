"""The remote-agent prompt (inlined files, reply frame) and reading the reply back."""
import pytest

from agy_bridge.core import remote_prompt
from agy_bridge.core.config import UsageError
from agy_bridge.core.stage import Entry

M = remote_prompt.Markers("STEM7XQ2AB")


def entries():
    return [Entry("src/a.py", b"def add(a, b):\r\n    return a + b", "0" * 64),
            Entry('odd "name".txt', b"\xef\xbb\xbfplain\n", "1" * 64)]


def test_prompt_inlines_every_file_between_tagged_delimiters():
    prompt = remote_prompt.build_prompt("Review it.", entries(), M)
    assert '----- BEGIN FILE "src/a.py" [STEM7XQ2AB] -----\ndef add(a, b):\n    return a + b\n' \
           '----- END FILE "src/a.py" [STEM7XQ2AB] -----' in prompt
    assert '----- BEGIN FILE "odd \\"name\\".txt" [STEM7XQ2AB] -----\nplain\n' in prompt
    assert "Task:\nReview it.\n" in prompt and "use no tools" in prompt


def test_the_prompt_names_the_markers_only_as_halves():
    prompt = remote_prompt.build_prompt("t", entries(), M)
    assert M.begin not in prompt and M.end not in prompt
    assert "joining STEM7XQ2AB and BEGIN" in prompt and "joining STEM7XQ2AB and END" in prompt


def test_a_file_or_task_holding_the_random_tag_is_refused():
    clash = [Entry("a", b"x [STEM7XQ2AB] y", "0" * 64)]
    with pytest.raises(UsageError):
        remote_prompt.build_prompt("t", clash, M)
    with pytest.raises(UsageError):
        remote_prompt.build_prompt("say " + M.end, entries(), M)


def test_new_markers_are_random():
    assert remote_prompt.new_markers().stem != remote_prompt.new_markers().stem


def test_extract_takes_the_last_begin_and_strips_the_margin():
    lines = ["> prompt echo joining STEM7XQ2AB and BEGIN", "  " + M.begin, "  stale", "  " + M.end, "",
             "▸ Thought for 2s", "  " + M.begin, "", "  FILE: a | QUOTE: \"x\" | CLAIM: y", "    indented",
             "", "  " + M.end, "  Sources:"]
    assert remote_prompt.extract_answer(lines, M) == 'FILE: a | QUOTE: "x" | CLAIM: y\n  indented\n'


def test_extract_needs_both_markers_in_order():
    assert remote_prompt.extract_answer(["  " + M.begin, "  text"], M) is None
    assert remote_prompt.extract_answer(["  " + M.end, "  " + M.begin], M) is None
    assert remote_prompt.extract_answer(["x" + M.begin, M.end], M) is None
    assert remote_prompt.extract_answer([M.begin, M.end], M) == ""
    assert remote_prompt.begun(["  " + M.begin], M) and not remote_prompt.begun(["nothing"], M)


def test_extract_drops_a_fence_left_on_screen_but_keeps_inner_fences():
    lines = ["  " + M.begin, "", "  ```text", "    a", "    ```", "    b", "  ```", "", "  " + M.end]
    assert remote_prompt.extract_answer(lines, M) == "a\n```\nb\n"
    rendered = ["  " + M.begin, "", "    FILE: a | QUOTE: \"def __init__(self):\" | CLAIM: x", "", "  " + M.end]
    assert remote_prompt.extract_answer(rendered, M) == 'FILE: a | QUOTE: "def __init__(self):" | CLAIM: x\n'
