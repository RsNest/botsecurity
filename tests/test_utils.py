from bot.utils import clamp


def test_clamp_closes_open_code_tag() -> None:
    # Force a cut inside an open <code>…</code> block.
    prefix = "head " * 10
    body = "<code>" + ("a" * 200) + "</code>"
    text = prefix + body
    limit = len(prefix) + 50  # mid-code
    out = clamp(text, limit=limit)
    assert out.count("<code>") == out.count("</code>")
    assert "Can't find end tag" not in out
    assert out.endswith("</code>") or "<code>" not in out


def test_clamp_short_text_unchanged() -> None:
    text = "hello <b>world</b>"
    assert clamp(text, limit=100) == text


def test_clamp_drops_incomplete_trailing_tag() -> None:
    text = "ok prefix " + ("x" * 40) + "<code"
    out = clamp(text, limit=30)
    assert "<code" not in out
    assert "…" in out
