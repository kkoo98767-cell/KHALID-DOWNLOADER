import pytest
from app.main import validate_url

@pytest.mark.parametrize("url", ["https://www.youtube.com/watch?v=sample", "https://www.tiktok.com/@user/video/123"])
def test_accepts_http_urls(url):
    assert validate_url(url) == url

@pytest.mark.parametrize("url", ["", "ftp://example.com/a", "http://localhost/a", "http://127.0.0.1/a"])
def test_rejects_invalid_or_local_urls(url):
    with pytest.raises(ValueError):
        validate_url(url)
