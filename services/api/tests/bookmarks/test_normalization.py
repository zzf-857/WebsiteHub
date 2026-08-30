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


def test_noncanonical_numeric_ipv4_hosts_are_never_classified_as_public() -> None:
    for url in (
        "http://2130706433/private",
        "http://0x7f000001/private",
        "http://0177.0.0.1/private",
        "http://127.1/private",
    ):
        normalized = normalize_bookmark_url(url)
        assert normalized.status is NormalizationStatus.ACCEPTED
        assert normalized.fetch_policy is FetchPolicy.EXPORT_METADATA_ONLY


def test_multicast_and_home_arpa_targets_are_never_classified_as_public() -> None:
    for url in (
        "http://224.0.0.1/service",
        "http://[ff02::1]/service",
        "https://home.arpa/admin",
        "https://router.home.arpa/admin",
    ):
        normalized = normalize_bookmark_url(url)
        assert normalized.status is NormalizationStatus.ACCEPTED
        assert normalized.fetch_policy is FetchPolicy.EXPORT_METADATA_ONLY


def test_backslash_url_parser_differentials_are_rejected() -> None:
    normalized = normalize_bookmark_url(r"http://2130706433\example.com/")

    assert normalized.status is NormalizationStatus.INVALID
    assert normalized.reason == "backslash_in_url"


def test_percent_encoded_authorities_are_rejected() -> None:
    normalized = normalize_bookmark_url("http://%31%32%37.0.0.1/")

    assert normalized.status is NormalizationStatus.INVALID
    assert normalized.reason == "encoded_authority"


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
