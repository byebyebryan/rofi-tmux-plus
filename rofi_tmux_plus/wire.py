"""Strict, bounded JSON wire helpers shared by public process boundaries."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import TextIO


class WireError(ValueError):
    """The bytes do not represent one canonical JSON document."""


def _strict_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise WireError("duplicate object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise WireError(f"non-finite JSON number: {value}")


def _parse_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise WireError("non-finite JSON number")
    return parsed


def decode_document(raw: bytes, *, limit: int) -> object:
    """Decode one strict UTF-8 JSON document followed by exactly one LF."""
    if not isinstance(raw, bytes):
        raise WireError("wire output is not bytes")
    if len(raw) > limit:
        raise WireError("wire output exceeded its limit")
    if b"\x00" in raw:
        raise WireError("embedded NUL")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise WireError("byte-order mark")
    if not raw.endswith(b"\n"):
        raise WireError("missing final line feed")
    document = raw[:-1]
    if document.endswith((b"\r", b"\n", b" ", b"\t")):
        raise WireError("extra final whitespace")
    try:
        text = document.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise WireError("invalid UTF-8") from error
    try:
        decoder = json.JSONDecoder(
            object_pairs_hook=_strict_pairs,
            parse_float=_parse_float,
            parse_constant=_reject_constant,
        )
        value, end = decoder.raw_decode(text)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise WireError("invalid JSON document") from error
    if end != len(text):
        raise WireError("trailing byte or extra document")
    return value


def encode_document(value: object, *, limit: int) -> bytes:
    """Encode a JSON value in the producer's canonical one-LF form."""
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        raw = text.encode("utf-8") + b"\n"
    except (TypeError, ValueError, UnicodeError) as error:
        raise WireError("value cannot be encoded as strict JSON") from error
    if len(raw) > limit:
        raise WireError("wire output exceeded its limit")
    return raw


def write_document(stream: TextIO, value: object, *, limit: int) -> bytes:
    """Write one encoded document to text or binary test/stdout streams."""
    raw = encode_document(value, limit=limit)
    binary = getattr(stream, "buffer", None)
    if binary is not None:
        binary.write(raw)
        if hasattr(binary, "flush"):
            binary.flush()
    else:
        # StringIO is used by the existing unit tests.  This path still
        # preserves the exact UTF-8 text and one final LF semantics.
        try:
            stream.write(raw)  # type: ignore[arg-type]
        except TypeError:
            stream.write(raw.decode("utf-8"))
        if hasattr(stream, "flush"):
            stream.flush()
    return raw


def validate_string_bounds(value: object, *, limit: int) -> None:
    """Reject oversized nested strings before response serialization."""
    if isinstance(value, str):
        if len(value) > limit:
            raise WireError("response string exceeded its limit")
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise WireError("response object key is not a string")
            if len(key) > limit:
                raise WireError("response object key exceeded its limit")
            validate_string_bounds(child, limit=limit)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            validate_string_bounds(child, limit=limit)
