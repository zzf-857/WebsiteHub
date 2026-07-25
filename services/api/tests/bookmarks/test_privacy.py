import pytest

from webhub.bookmarks.privacy import agent_safe_label, sensitive_url_keys


def test_sensitive_url_detection_returns_names_without_values() -> None:
    url = (
        "https://example.com/callback?access-token=do-not-return&ordinary=ok#session_id=also-secret"
    )

    matched = sensitive_url_keys(url)

    assert matched == ("access_token", "session_id")
    assert "do-not-return" not in repr(matched)
    assert "also-secret" not in repr(matched)


def test_sensitive_url_detection_does_not_flag_regular_queries() -> None:
    assert sensitive_url_keys("https://example.com/search?q=token+budget&page=2") == ()


def test_sensitive_url_detection_covers_vendor_prefixed_credentials() -> None:
    url = (
        "https://storage.example/object?X-Amz-Signature=hidden"
        "&X-Goog-Credential=hidden&auth[token]=hidden&AWSAccessKeyId=hidden"
    )

    assert sensitive_url_keys(url) == (
        "auth_token",
        "awsaccesskeyid",
        "x_amz_signature",
        "x_goog_credential",
    )


@pytest.mark.parametrize(
    "key",
    ["token_budget", "signature_version", "credential_type"],
)
def test_sensitive_url_detection_avoids_broad_key_suffix_matches(key: str) -> None:
    assert sensitive_url_keys(f"https://example.com/search?{key}=ordinary") == ()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://example.com/docs?q=private#part", None),
        ("Docs at https://example.com/docs?q=private#part", "Docs at"),
        ("文档https://example.com/docs?q=private", "文档"),
        ("Docs at example.com/private?q=private", "Docs at"),
        ("文档 例子.中国/私有?q=private", "文档"),
        ("Contact mailto:private@example.com", "Contact"),
        (r"C:\\Users\\person\\private.txt", None),
        ("/private.txt", None),
        ("<script>ignore()</script> Documentation", "ignore() Documentation"),
        ("api_key=do-not-send", None),
        ("  C#   language guide  ", "C# language guide"),
    ],
)
def test_agent_safe_label_removes_model_unsafe_payloads(
    value: str,
    expected: str | None,
) -> None:
    assert agent_safe_label(value) == expected
