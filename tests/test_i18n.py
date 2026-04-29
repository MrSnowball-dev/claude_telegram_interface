from pathlib import Path

import pytest

from bot import i18n


@pytest.fixture(autouse=True, scope="module")
def _load() -> None:
    i18n.load_locales(Path(__file__).resolve().parent.parent / "bot" / "locales")


def test_languages_share_keys() -> None:
    base = set(i18n._LOCALES["en"])  # noqa: SLF001
    for code, strings in i18n._LOCALES.items():  # noqa: SLF001
        assert set(strings) == base, f"locale {code} drifts: missing={base - strings.keys()} extra={strings.keys() - base}"


def test_pick_lang_handles_variants() -> None:
    assert i18n.pick_lang("ru-RU") == "ru"
    assert i18n.pick_lang("uk") == "uk"
    assert i18n.pick_lang("zh-CN") == "en"
    assert i18n.pick_lang(None) == "en"
    assert i18n.pick_lang("") == "en"


def test_t_falls_back_to_default() -> None:
    # A key that exists in en should always resolve, even for an unknown lang.
    assert i18n.t("welcome", "xx").startswith(("Hi.", "Привет.", "Привіт."))


def test_t_formats_placeholders() -> None:
    out = i18n.t("cd_ok", "en", path="/x/y")
    assert "/x/y" in out
