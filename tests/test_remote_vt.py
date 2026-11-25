"""The pure VT screen emulator used to read agy's remote-agent terminal UI."""
from agy_bridge.core.vt_screen import Screen, render


def feed_bytewise(screen, data):
    for i in range(len(data)):
        screen.feed(data[i:i + 1])


def test_cursor_movement_rebuilds_text_that_escape_stripping_would_scramble():
    data = b"\x1b[6CWorld\rHello \r\n"
    assert render(data) == "Hello World"
    assert render(b"abcdef\x1b[3D\x1b[KXY") == "abcXY"


def test_escapes_and_utf8_split_across_chunks_are_kept_whole():
    screen = Screen(5, 40)
    feed_bytewise(screen, "zażółć \x1b[1;1H\x1b[31mZ\x1b[0m".encode("utf-8"))
    assert screen.lines() == ["Zażółć"]


def test_rows_that_scroll_off_the_top_are_kept_in_the_scrollback():
    screen = Screen(4, 20)
    screen.feed(b"".join(b"line %d\r\n" % i for i in range(30)))
    assert screen.lines() == ["line %d" % i for i in range(30)]
    assert len(screen.history) == 27


def test_scroll_inside_a_region_does_not_touch_the_scrollback():
    screen = Screen(5, 20)
    screen.feed(b"top\x1b[2;4r\x1b[2;1Ha\r\nb\r\nc\r\nd\r\n")
    assert screen.history == []
    assert screen.screen_lines()[0] == "top"


def test_alternate_screen_is_left_out_and_the_main_screen_restored():
    screen = Screen(5, 30)
    screen.feed(b"main text\r\n\x1b[?1049h\x1b[HTrust this folder?")
    assert screen.alternate and screen.lines() == ["main text"]
    screen.feed(b"\x1b[?1049lafter")
    assert screen.lines() == ["main text", "after"]


def test_rows_wrapped_at_the_margin_are_joined_again():
    screen = Screen(4, 10)
    screen.feed(b"0123456789abcdefghij-tail\r\nnext")
    assert screen.lines() == ["0123456789abcdefghij-tail", "next"]


def test_terminal_queries_get_answers_for_the_program():
    screen = Screen(5, 20)
    screen.feed(b"ab\x1b[6n\x1b[c\x1b[>c\x1b[?2026$p\x1b]11;?\x07")
    replies = screen.pending_replies()
    assert b"\x1b[1;3R" in replies and b"\x1b[?62;22c" in replies and b"\x1b[>0;0;0c" in replies
    assert b"\x1b[?2026;0$y" in replies and b"\x1b]11;rgb:" in replies
    assert screen.pending_replies() == b""


def test_erase_insert_and_delete_work_on_the_cursor_row():
    assert render(b"hello\x1b[1G\x1b[2P") == "llo"
    assert render(b"hello\x1b[1G\x1b[2@") == "  hello"
    assert render(b"hello\x1b[2G\x1b[3X") == "h   o"
    assert render(b"a\r\nb\r\nc\x1b[2;1H\x1b[M") == "a\nc"
    assert render(b"a\r\nb\x1b[1;1H\x1b[L") == "\na\nb"
    assert render(b"one\r\ntwo\x1b[2J") == ""


def test_wide_and_combining_characters_take_their_cells():
    assert render("日本X".encode("utf-8")) == "日本X"
    assert render("éx".encode("utf-8")) == "éx"


def test_malformed_or_endless_escapes_do_not_swallow_text():
    assert render(b"a\x1b\x01b") == "ab"
    screen = Screen(3, 20)
    screen.feed(b"\x1b]0;" + b"x" * 5000)
    screen.feed(b"visible")
    assert "visible" in screen.text()


def test_erase_scrollback_is_ignored_so_the_record_survives():
    screen = Screen(3, 20)
    screen.feed(b"".join(b"r%d\r\n" % i for i in range(6)) + b"\x1b[3J")
    assert screen.lines()[0] == "r0"
