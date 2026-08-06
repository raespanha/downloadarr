import base64

import pytest

from downloadarr.magnets import MagnetError, parse_magnet

HASH = "0123456789abcdef0123456789abcdef01234567"


def test_hex_magnet_and_name():
    value = parse_magnet(f"magnet:?dn=My%20Release&xt=urn:btih:{HASH.upper()}&tr=https://secret")
    assert value.info_hash == HASH
    assert value.display_name == "My Release"


def test_base32_hash_normalizes():
    encoded = base64.b32encode(bytes.fromhex(HASH)).decode()
    assert parse_magnet(f"magnet:?xt=urn:btih:{encoded}").info_hash == HASH


@pytest.mark.parametrize("uri", ["https://example.test", "magnet:?dn=nohash",
                                  "magnet:?xt=urn:btih:bad"])
def test_invalid_magnets(uri):
    with pytest.raises(MagnetError):
        parse_magnet(uri)


def test_conflicting_hashes_rejected():
    with pytest.raises(MagnetError):
        parse_magnet(f"magnet:?xt=urn:btih:{HASH}&xt=urn:btih:{'f' * 40}")
