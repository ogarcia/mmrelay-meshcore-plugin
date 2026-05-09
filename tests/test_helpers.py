import pytest


class TestSanitizeText:
    def test_returns_empty_string_for_none(self):
        from plugin import sanitize_text
        assert sanitize_text(None) == ""

    def test_normalizes_unicode(self):
        from plugin import sanitize_text
        assert sanitize_text("café") == sanitize_text("café")

    def test_removes_control_characters(self):
        from plugin import sanitize_text
        assert sanitize_text("hello\x00world") == "helloworld"
        assert sanitize_text("test\x07bell") == "testbell"

    def test_removes_tabs(self):
        from plugin import sanitize_text
        assert sanitize_text("hello\tworld") == "hello world"

    def test_removes_zero_width_chars(self):
        from plugin import sanitize_text
        assert sanitize_text("hello\u200bworld") == "helloworld"
        assert sanitize_text("test\ufeffdata") == "testdata"

    def test_trims_whitespace(self):
        from plugin import sanitize_text
        assert sanitize_text("  hello  ") == "hello"
        assert sanitize_text("\t\thello\t\t") == "hello"

    def test_truncates_long_messages(self):
        from plugin import sanitize_text
        long_text = "a" * 300
        result = sanitize_text(long_text)
        assert len(result.encode("utf-8")) <= 200
        assert result.endswith("…")

    def test_preserves_newlines(self):
        from plugin import sanitize_text
        result = sanitize_text("line1\nline2")
        assert "\n" in result


class TestHasTimestampPrefix:
    def test_returns_true_for_valid_prefix(self):
        from plugin import has_timestamp_prefix
        assert has_timestamp_prefix("[1a2b3c] message") is True

    def test_returns_false_for_no_prefix(self):
        from plugin import has_timestamp_prefix
        assert has_timestamp_prefix("plain message") is False
        assert has_timestamp_prefix("") is False

    def test_returns_false_for_malformed_prefix(self):
        from plugin import has_timestamp_prefix
        assert has_timestamp_prefix("[nothex] message") is False

    def test_handles_none(self):
        from plugin import has_timestamp_prefix
        assert has_timestamp_prefix(None) is False


class TestComputeChannelId:
    def test_returns_key_for_named_channel(self):
        from plugin import compute_channel_id
        key = "F32E1D081E0FE4C4849BE4324BE2CBD9"
        result = compute_channel_id("GALICIA", key)
        assert result == key

    def test_returns_sha256_for_public_channel(self):
        from plugin import compute_channel_id
        import hashlib
        expected = hashlib.sha256("#Public".encode("utf-8")).digest()[:16].hex().upper()
        result = compute_channel_id("#Public", "")
        assert result == expected

    def test_normalizes_key_to_uppercase(self):
        from plugin import compute_channel_id
        key = "f32e1d081e0fe4c4849be4324be2cbd9"
        result = compute_channel_id("GALICIA", key)
        assert result == key.upper()

    def test_raises_for_invalid_key(self):
        from plugin import compute_channel_id
        with pytest.raises(ValueError):
            compute_channel_id("GALICIA", "invalid")

    def test_handles_none_name(self):
        from plugin import compute_channel_id
        result = compute_channel_id(None, "")
        assert result is not None

    def test_strips_whitespace(self):
        from plugin import compute_channel_id
        key = "F32E1D081E0FE4C4849BE4324BE2CBD9"
        result = compute_channel_id("  GALICIA  ", key)
        assert result == key


class TestDecryptGroupText:
    def test_returns_none_for_short_payload(self):
        from plugin import decrypt_group_text
        assert decrypt_group_text(b"ab", b"0" * 32) is None

    def test_returns_none_when_aes_unavailable(self, monkeypatch):
        import plugin
        monkeypatch.setattr(plugin, "AES", None)
        from plugin import decrypt_group_text
        assert decrypt_group_text(b"abc" * 5, b"0" * 32) is None

    def test_returns_none_for_invalid_padding(self):
        from plugin import decrypt_group_text
        payload = b"\x00\x01\x02" + b"a"
        assert decrypt_group_text(payload, b"0" * 32) is None


class TestParseChannelMapping:
    def test_returns_none_for_missing_room(self):
        from plugin import parse_channel_mapping
        result = parse_channel_mapping({"meshcore_channel_name": "GALICIA"})
        assert result is None

    def test_returns_none_for_missing_name(self):
        from plugin import parse_channel_mapping
        result = parse_channel_mapping({"matrix_room": "!roomid:example.org"})
        assert result is None

    def test_parses_valid_mapping(self):
        from plugin import parse_channel_mapping
        result = parse_channel_mapping({
            "matrix_room": "!roomid:example.org",
            "meshcore_channel_name": "GALICIA",
            "meshcore_channel_key": "F32E1D081E0FE4C4849BE4324BE2CBD9",
        })
        assert result["matrix_room"] == "!roomid:example.org"
        assert result["channel_name"] == "GALICIA"
        assert result["channel_key"] == "F32E1D081E0FE4C4849BE4324BE2CBD9"

    def test_canonicalizes_hashtag_name(self):
        from plugin import parse_channel_mapping
        result = parse_channel_mapping({
            "matrix_room": "!roomid:example.org",
            "meshcore_channel_name": "#Public",
        })
        assert result["channel_name"] == "Public"

    def test_auto_fills_public_key(self):
        from plugin import parse_channel_mapping
        result = parse_channel_mapping({
            "matrix_room": "!roomid:example.org",
            "meshcore_channel_name": "#public",
        })
        assert result["channel_key"] == "8b3387e9c5cdea6ac9e5edbaa115cd72"

    def test_parses_channel_index(self):
        from plugin import parse_channel_mapping
        result = parse_channel_mapping({
            "matrix_room": "!roomid:example.org",
            "meshcore_channel_name": "GALICIA",
            "meshcore_channel_index": 5,
        })
        assert result["channel_index"] == 5

    def test_ignores_invalid_channel_index(self):
        from plugin import parse_channel_mapping
        result = parse_channel_mapping({
            "matrix_room": "!roomid:example.org",
            "meshcore_channel_name": "GALICIA",
            "meshcore_channel_index": "not-a-number",
        })
        assert "channel_index" not in result
