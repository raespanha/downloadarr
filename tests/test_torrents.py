import hashlib

import pytest

from downloadarr.torrents import MAX_TORRENT_BYTES, TorrentError, parse_torrent


INFO = (b"d6:lengthi5e4:name12:Test.Release12:piece lengthi16384e"
        b"6:pieces20:abcdefghijklmnopqrste")
TORRENT = b"d4:info" + INFO + b"e"


def test_parses_v1_torrent_using_raw_info_dictionary():
    parsed = parse_torrent(TORRENT, r"C:\incoming\release")
    assert parsed.info_hash == hashlib.sha1(INFO).hexdigest()
    assert parsed.display_name == "Test.Release"
    assert parsed.filename == "release.torrent"
    assert parsed.payload == TORRENT


@pytest.mark.parametrize("payload", [
    b"", b"not-bencode", b"d4:infodejunk", b"d4:infod4:name1:xee",
    b"d4:infod6:pieces20:abcdefghijklmnopqrst6:lengthi+5eee",
])
def test_rejects_empty_malformed_and_v2_only_torrents(payload):
    with pytest.raises(TorrentError):
        parse_torrent(payload)


def test_rejects_oversized_torrent():
    with pytest.raises(TorrentError, match="exceeds"):
        parse_torrent(bytes(MAX_TORRENT_BYTES + 1))
