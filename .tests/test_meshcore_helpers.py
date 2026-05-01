import pytest
from meshcore_helpers import sanitize_text, _MAX_MSG_LEN

def test_returns_str_never_none():
    assert isinstance(sanitize_text(None), str)
    assert sanitize_text(None) == ""
    assert isinstance(sanitize_text(""), str)
    assert sanitize_text("") == ""
    assert isinstance(sanitize_text("hello"), str)


def test_normalization_and_controls():
    # Normalization
    assert sanitize_text("\u212B") == "Å"  # Angstrom
    # Strips C0/C1 except \n
    in_text = "\x01foo\tbar\x1f"
    out = sanitize_text(in_text)
    assert "\x01" not in out and "\x1f" not in out
    assert "foo" in out and "bar" in out
    assert "\t" not in out

    # Zero width
    assert sanitize_text("abc\u200bdef") == "abcdef"

    # Strip whitespace
    assert sanitize_text("   abc   ") == "abc"
    assert sanitize_text("abc\n ") == "abc\n"


def test_empty_and_blank():
    assert sanitize_text("") == ""
    assert sanitize_text("   ") == ""


def test_long_text_truncation():
    # Build slightly over-max bytes (all ascii)
    s = "a" * (_MAX_MSG_LEN + 30)
    out = sanitize_text(s)
    assert isinstance(out, str)
    assert len(out.encode("utf-8")) <= _MAX_MSG_LEN
    # Should have ellipsis if cut
    if len(out) < len(s):
        assert out.endswith("…")
    # Build multi-byte unicode just over
    s = "á" * (_MAX_MSG_LEN//2)  # > _MAX_MSG_LEN bytes
    out = sanitize_text(s)
    assert len(out.encode("utf-8")) <= _MAX_MSG_LEN
    # No split-character
    assert out == out.encode().decode()


def test_newline_preserved():
    t = "hello\nworld"
    assert "\n" in sanitize_text(t)


def test_high_unicode():
    # Emojis
    s = "start 🚀" * 50
    out = sanitize_text(s)
    assert isinstance(out, str)
    assert len(out.encode("utf-8")) <= _MAX_MSG_LEN

    # Remove known control
    bad = "test\x00\tend"
    clean = sanitize_text(bad)
    assert "\x00" not in clean and "\t" not in clean


def test_no_break_on_control_edge():
    # A string ending on a control character after trunc
    s = ("好" * 3000) + "\x05"
    out = sanitize_text(s)
    assert isinstance(out, str) and "\x05" not in out

# To run: pytest .tests/test_meshcore_helpers.py
