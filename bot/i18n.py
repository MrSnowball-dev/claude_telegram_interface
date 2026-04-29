"""TOML-backed translations. Drop a new ``locales/<code>.toml`` in to add a language."""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path

log = logging.getLogger(__name__)

_DEFAULT_LANG = "en"
_LOCALES: dict[str, dict[str, str]] = {}
_NAMES: dict[str, str] = {}


def load_locales(directory: Path) -> None:
    _LOCALES.clear()
    _NAMES.clear()
    for path in sorted(directory.glob("*.toml")):
        with path.open("rb") as f:
            data = tomllib.load(f)
        code = path.stem
        _LOCALES[code] = dict(data.get("strings", {}))
        _NAMES[code] = str(data.get("meta", {}).get("name", code))

    if _DEFAULT_LANG not in _LOCALES:
        raise RuntimeError(f"Default locale '{_DEFAULT_LANG}.toml' not found in {directory}")

    expected = set(_LOCALES[_DEFAULT_LANG])
    for code, strings in _LOCALES.items():
        missing = expected - strings.keys()
        extra = strings.keys() - expected
        if missing:
            log.warning("locale %s missing keys: %s", code, sorted(missing))
        if extra:
            log.warning("locale %s has extra keys: %s", code, sorted(extra))


def available_languages() -> dict[str, str]:
    return dict(_NAMES)


def pick_lang(tg_lang_code: str | None) -> str:
    if not tg_lang_code:
        return _DEFAULT_LANG
    short = tg_lang_code.split("-", 1)[0].lower()
    return short if short in _LOCALES else _DEFAULT_LANG


def t(key: str, lang: str, /, **fmt: object) -> str:
    val = _LOCALES.get(lang, {}).get(key)
    if val is None:
        val = _LOCALES.get(_DEFAULT_LANG, {}).get(key)
    if val is None:
        log.error("missing translation key: %s", key)
        return f"<{key}>"
    if not fmt:
        return val
    try:
        return val.format(**fmt)
    except (KeyError, IndexError) as e:
        log.error("format error in key %s/%s: %s", lang, key, e)
        return val
