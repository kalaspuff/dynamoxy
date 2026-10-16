"""Pagination cursors."""

from __future__ import annotations

import base64
import json
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dynamoxy.query import decode_cursor, encode_cursor

key_values = st.one_of(
    st.text(max_size=20),
    st.integers(-(10**20), 10**20),
    st.decimals(allow_nan=False, allow_infinity=False, places=3, min_value=-(10**6), max_value=10**6),
    st.binary(max_size=16),
)


@given(st.dictionaries(st.sampled_from(["pk", "sk", "gsi1pk", "gsi1sk"]), key_values, min_size=1))
def test_cursors_round_trip(key: dict[str, Any]) -> None:
    cursor = encode_cursor(key)
    assert cursor is not None
    assert "=" not in cursor
    assert "+" not in cursor
    assert "/" not in cursor
    decoded = decode_cursor(cursor)
    expected = {
        name: int(value) if isinstance(value, Decimal) and value == value.to_integral_value() else value
        for name, value in key.items()
    }
    assert decoded == expected


def test_no_key_means_no_cursor() -> None:
    assert encode_cursor(None) is None


@pytest.mark.parametrize("cursor", ["", "%%%", "bm90IGpzb24", "WzFd", "eyJhIjogeyJYIjogMX19"])
def test_invalid_cursors(cursor: str) -> None:
    with pytest.raises(ValueError, match="invalid cursor"):
        decode_cursor(cursor)


def test_invalid_cursors_do_not_quote_their_contents() -> None:
    # A cursor holds key values, which may be personal data.
    cursor = base64.urlsafe_b64encode(json.dumps({"pk": {"N": "ada@example.com"}}).encode()).decode()
    with pytest.raises(ValueError, match="invalid cursor") as caught:
        decode_cursor(cursor)
    assert "ada@example.com" not in str(caught.value)
