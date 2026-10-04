"""Theme helpers: the hex-alpha format Flet's client actually parses."""

from app import theme as T


def test_alpha_emits_a_hash_prefixed_aarrggbb_string():
    """Regression: dropping the ``#`` made the client fall back to black.

    The sweep wedge painted solid black for exactly this reason — every
    ``T.alpha()`` call site (22 of them) depends on this shape.
    """
    assert T.alpha(T.ACCENT, 1.0) == "#FF30E8F8"
    assert T.alpha(T.ACCENT, 0.55) == "#8C30E8F8"
    assert T.alpha("#0B1B33", 0.0) == "#000B1B33"
    assert T.alpha("30e8f8", 0.0) == "#0030E8F8"  # a missing/low-case # is fine


def test_alpha_clamps_the_opacity():
    assert T.alpha(T.ACCENT, -3) == "#0030E8F8"
    assert T.alpha(T.ACCENT, 9) == "#FF30E8F8"


def test_avatar_fill_is_stable_for_a_given_key():
    assert T.avatar_fill("peer-abc") == T.avatar_fill("peer-abc")
    assert T.avatar_fill("peer-abc").startswith("#")


def test_initials_come_from_the_display_name():
    assert T.initials("Maya's Laptop") == "ML"
    assert T.initials("studio-pc") == "ST"
    assert T.initials("") == "?"  # never raises, never renders a blank badge
