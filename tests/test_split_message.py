from main import split_message


def test_single_overlong_line_is_sliced_to_limit():
    chunks = split_message("a" * 25, limit=10)
    assert chunks == ["a" * 10, "a" * 10, "a" * 5]


def test_overlong_line_after_short_lines_does_not_exceed_limit():
    chunks = split_message("short\n" + "b" * 25 + "\nend", limit=10)
    assert all(len(c) <= 10 for c in chunks)
    assert chunks[0] == "short"
    assert "".join(chunks).replace("\n", "") == "short" + "b" * 25 + "end"


def test_blank_lines_are_preserved():
    assert split_message("a\n\nb", limit=10) == ["a\n\nb"]


def test_normal_text_splits_on_line_boundaries():
    assert split_message("aaaa\nbbbb\ncccc", limit=9) == ["aaaa\nbbbb", "cccc"]
