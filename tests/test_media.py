import pytest

from invoiceguard.media import detect_media_type


@pytest.mark.parametrize("data,expected", [
    (b"%PDF-1.4 rest", "application/pdf"), (b"\x89PNG\r\n\x1a\nxx", "image/png"), (b"\xff\xd8\xff\xe0xx", "image/jpeg"),
    (b"GIF89a....", "image/gif"), (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
    (b"", None), (b"hello world", None), (b"RIFF\x00\x00\x00\x00WAVEfmt ", None), (b"<html>%PDF-</html>", None),
])
def test_detect(data, expected):
    assert detect_media_type(data) == expected
