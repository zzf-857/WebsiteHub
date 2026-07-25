from webhub.bookmarks.models import FetchPolicy, NormalizationStatus
from webhub.bookmarks.normalization import normalize_bookmark_url


def test_normalization_is_conservative_and_preserves_resource_identity() -> None:
    first = normalize_bookmark_url("HTTPS://Example.COM.:443/path?a=2&a=1#first")
    second = normalize_bookmark_url("https://example.com/path?a=2&a=1#second")

    assert first.status is NormalizationStatus.ACCEPTED
    assert first.normalized_url == "https://example.com/path?a=2&a=1#first"
    assert second.normalized_url == "https://example.com/path?a=2&a=1#second"
    assert first.normalized_url != second.normalized_url


def test_normalization_marks_local_targets_as_export_metadata_only() -> None:
    local = normalize_bookmark_url("http://127.0.0.1:8080/dashboard")
    public = normalize_bookmark_url("https://example.com")

    assert local.status is NormalizationStatus.ACCEPTED
    assert local.fetch_policy is FetchPolicy.EXPORT_METADATA_ONLY
    assert public.normalized_url == "https://example.com/"
    assert public.fetch_policy is FetchPolicy.PUBLIC_REVALIDATION_REQUIRED


def test_normalization_rejects_unsafe_or_non_web_urls() -> None:
    local_file = normalize_bookmark_url("file:///C:/secret.txt")
    credentials = normalize_bookmark_url("https://user:password@example.com/private")
    browser_page = normalize_bookmark_url("chrome://dino/")
    whitespace = normalize_bookmark_url("https://example.com/bad path")

    assert local_file.status is NormalizationStatus.UNSUPPORTED
    assert local_file.reason == "unsupported_scheme:file"
    assert browser_page.status is NormalizationStatus.UNSUPPORTED
    assert credentials.status is NormalizationStatus.INVALID
    assert credentials.reason == "embedded_credentials"
    assert whitespace.status is NormalizationStatus.INVALID
