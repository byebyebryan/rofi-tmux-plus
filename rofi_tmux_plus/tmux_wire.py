"""Strict decoding for tmux's ``#{q/a:...}`` and option snapshots.

The q/a formatter emits one tmux command argument.  It is deliberately not a
shell protocol: tmux can use either a quoted argument or an unquoted argument
with tmux escapes.  Keeping this tiny decoder separate makes the fast local
and remote inventory collectors agree on what constitutes a safe record.
"""

from __future__ import annotations

from collections.abc import Iterable


class TmuxWireError(ValueError):
    """The producer did not emit one bounded, unambiguous tmux argument."""


def _encoded_size(value: str) -> int:
    return len(value.encode("utf-8", errors="strict"))


def _decode_escape(value: str, index: int) -> tuple[str, int]:
    """Decode one tmux command-argument escape beginning after ``\\``."""
    if index >= len(value):
        raise TmuxWireError("tmux argument ends with an escape")
    char = value[index]
    simple = {"e": "\x1b", "r": "\r", "n": "\n", "t": "\t"}
    if char in simple:
        return simple[char], index + 1
    if char in "01234567":
        end = index + 3
        if end > len(value) or any(item not in "01234567" for item in value[index:end]):
            raise TmuxWireError("tmux argument has an invalid octal escape")
        return chr(int(value[index:end], 8)), end
    if char == "u":
        # tmux accepts either four or eight hexadecimal digits. Prefer the
        # eight-digit form whenever it is unambiguous, exactly like its parser.
        available = value[index + 1 :]
        width = (
            8
            if len(available) >= 8
            and all(item in "0123456789abcdefABCDEF" for item in available[:8])
            else 4
        )
        encoded = available[:width]
        if len(encoded) != width or any(item not in "0123456789abcdefABCDEF" for item in encoded):
            raise TmuxWireError("tmux argument has an invalid Unicode escape")
        codepoint = int(encoded, 16)
        if codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
            raise TmuxWireError("tmux argument has an invalid Unicode escape")
        return chr(codepoint), index + 1 + width
    # tmux removes a backslash before any other character.
    return char, index + 1


def decode_tmux_argument(value: str, *, max_bytes: int = 16 * 1024) -> str:
    """Decode exactly one tmux command argument without evaluating it.

    q/a must never expose literal tabs or newlines.  Rejecting them is what
    makes callers' tab/newline record framing trustworthy, and a failure makes
    them select their established safe collector instead.
    """
    if not isinstance(value, str) or _encoded_size(value) > max_bytes * 4:
        raise TmuxWireError("tmux argument exceeds the framing limit")
    # tmux emits an empty expansion (rather than ``''`` or ``\"\"``) for an
    # empty format value under q/a. Callers already have an exact field count,
    # so this remains unambiguous at the record layer.
    if not value:
        return ""
    quote: str | None = None
    index = 0
    if value[0] in {"'", '"'}:
        quote = value[0]
        index = 1
    result: list[str] = []
    while index < len(value):
        char = value[index]
        if quote is not None and char == quote:
            index += 1
            if index != len(value):
                raise TmuxWireError("tmux argument has trailing data after a quote")
            break
        if quote is None and char in {"'", '"'}:
            raise TmuxWireError("tmux argument has an unexpected quote")
        if char == "\\" and quote != "'":
            decoded, index = _decode_escape(value, index + 1)
            result.append(decoded)
            continue
        if ord(char) < 0x20 or char == "\x7f":
            raise TmuxWireError("tmux argument contains an unescaped control character")
        if quote is None and char.isspace():
            raise TmuxWireError("tmux argument contains unquoted whitespace")
        result.append(char)
        index += 1
    else:
        if quote is not None:
            raise TmuxWireError("tmux argument has an unterminated quote")
    decoded = "".join(result)
    if _encoded_size(decoded) > max_bytes:
        raise TmuxWireError("tmux argument exceeds the framing limit")
    return decoded


def parse_explicit_user_options(
    output: str,
    names: Iterable[str],
    *,
    pending_name: str,
    max_bytes: int = 64 * 1024,
) -> tuple[bool, dict[str, str | None]]:
    """Read explicit session options from one ``show-options -q`` snapshot.

    ``-q`` intentionally omits inherited/global values.  The returned mapping
    therefore distinguishes an absent requested option (``None``) from an
    explicitly set empty option (``""``).
    """
    requested = tuple(dict.fromkeys(names))
    relevant = set(requested)
    relevant.add(pending_name)
    if _encoded_size(output) > max_bytes:
        raise TmuxWireError("tmux option snapshot exceeds the framing limit")
    values: dict[str, str] = {}
    for line in output.splitlines():
        if not line:
            raise TmuxWireError("tmux option snapshot contains an empty record")
        name, separator, encoded = line.partition(" ")
        if name not in relevant:
            continue
        if not separator or name in values:
            raise TmuxWireError("tmux option snapshot is malformed")
        values[name] = decode_tmux_argument(encoded)
    return pending_name in values, {name: values.get(name) for name in requested}
