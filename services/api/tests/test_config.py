from webhub.config import REPOSITORY_ROOT, get_settings


def test_relative_data_directory_is_anchored_to_repository(
    monkeypatch,
) -> None:
    monkeypatch.setenv("WEBHUB_DATA_DIR", "relative-data-test")
    get_settings.cache_clear()
    try:
        assert get_settings().data_directory == (REPOSITORY_ROOT / "relative-data-test").resolve()
    finally:
        get_settings.cache_clear()
