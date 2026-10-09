"""Just enough bencode to read a .torrent: its infohash, name and size."""

import hashlib


class BencodeError(ValueError):
    pass


def _decode(data: bytes, i: int):
    c = data[i:i + 1]
    if c == b"i":
        end = data.index(b"e", i)
        return int(data[i + 1:end]), end + 1
    if c == b"l":
        i += 1
        out = []
        while data[i:i + 1] != b"e":
            v, i = _decode(data, i)
            out.append(v)
        return out, i + 1
    if c == b"d":
        i += 1
        out = {}
        while data[i:i + 1] != b"e":
            k, i = _decode(data, i)
            v, i = _decode(data, i)
            out[k] = v
        return out, i + 1
    if c.isdigit():
        colon = data.index(b":", i)
        n = int(data[i:colon])
        start = colon + 1
        return data[start:start + n], start + n
    raise BencodeError(f"unexpected byte {c!r} at {i}")


def decode(data: bytes):
    try:
        value, end = _decode(data, 0)
    except (IndexError, ValueError) as e:
        raise BencodeError(str(e)) from e
    if end != len(data):
        raise BencodeError("trailing data")
    return value


def _info_span(data: bytes) -> tuple[int, int]:
    """Byte span of the top-level info dict, hashed as-is (never re-encoded)."""
    if data[:1] != b"d":
        raise BencodeError("not a dictionary")
    i = 1
    while data[i:i + 1] != b"e":
        key, i = _decode(data, i)
        start = i
        _, i = _decode(data, i)
        if key == b"info":
            return start, i
    raise BencodeError("no info dictionary")


def _encode(value) -> bytes:
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, str):
        enc = value.encode("utf-8")
        return str(len(enc)).encode() + b":" + enc
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, list):
        return b"l" + b"".join(_encode(v) for v in value) + b"e"
    if isinstance(value, dict):
        # bencode dicts must have sorted keys
        out = b"d"
        for k in sorted(value.keys()):
            out += _encode(k) + _encode(value[k])
        return out + b"e"
    raise BencodeError(f"cannot encode {type(value)}")


def encode(value) -> bytes:
    return _encode(value)


def _rewrite_announce_url(url: bytes, proxy_base: str, register) -> bytes:
    """Point an http(s) announce at the local proxy; the real URL (which carries
    the account's key) stays in the app, referenced by a short token."""
    u = url.decode("utf-8", "replace")
    if not u.startswith(("http://", "https://")):
        return url
    return f"{proxy_base.rstrip('/')}/proxy/ann?t={register(u)}".encode()


def redirect_announce(data: bytes, proxy_base: str, register) -> bytes:
    """Rewrite all announce URLs to go through the local announce proxy.

    The proxy strips downloaded bytes from every announce, so the tracker
    cannot attribute downloaded data to this account.  The passkey is carried
    server-side inside the proxy so qBittorrent never sees it.

    The info dict is left byte-for-byte identical so the infohash is preserved.
    """
    torrent = decode(data)
    if not isinstance(torrent, dict):
        raise BencodeError("top-level value is not a dict")

    if b"announce" in torrent:
        torrent[b"announce"] = _rewrite_announce_url(torrent[b"announce"], proxy_base, register)

    if b"announce-list" in torrent:
        torrent[b"announce-list"] = [
            [_rewrite_announce_url(u, proxy_base, register) for u in tier]
            for tier in torrent[b"announce-list"]
        ]

    start, end = _info_span(data)
    original_info_bytes = data[start:end]

    torrent[b"info"] = b"__PLACEHOLDER__"
    encoded = _encode(torrent)
    placeholder = _encode(b"__PLACEHOLDER__")
    info_key = _encode(b"info")
    needle = info_key + placeholder
    if needle not in encoded:
        raise BencodeError("could not locate info placeholder in encoded output")
    return encoded.replace(needle, info_key + original_info_bytes, 1)


def torrent_meta(data: bytes) -> dict:
    """infohash (hex), name and total size of a .torrent file."""
    try:
        start, end = _info_span(data)
        info = decode(data[start:end])
    except (IndexError, ValueError) as e:
        raise BencodeError(str(e)) from e

    if b"length" in info:
        size = info[b"length"]
    else:
        size = sum(f.get(b"length", 0) for f in info.get(b"files", []))

    return {
        "infohash": hashlib.sha1(data[start:end]).hexdigest(),
        "name": info.get(b"name", b"").decode("utf-8", "replace"),
        "size": size,
    }
