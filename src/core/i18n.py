"""Small language helpers shared by models, views and the translation service."""
from django.utils import translation

# Portal language codes and the suffix used by multilingual model fields (name_en, name_zh, name_ur).
LANG_FIELD_SUFFIX = {"en": "en", "zh-hans": "zh", "ur": "ur"}
PORTAL_LANGS = tuple(LANG_FIELD_SUFFIX)


def current_lang():
    lang = (translation.get_language() or "en").lower()
    if lang.startswith("zh"):
        return "zh-hans"
    if lang.startswith("ur"):
        return "ur"
    return "en"


def localized_attr(obj, base, lang=None):
    """Return obj.<base>_<lang>, falling back to English when the translation is empty."""
    suffix = LANG_FIELD_SUFFIX.get(lang or current_lang(), "en")
    value = getattr(obj, f"{base}_{suffix}", "") or ""
    return value or getattr(obj, f"{base}_en", "")


def detect_language(text):
    """
    Guess the language of free text from its script. With only English,
    Chinese and Urdu in play, the script is a reliable signal and needs no
    external service: Han characters -> zh-hans, Arabic script -> ur, else en.
    """
    han = arabic = latin = 0
    for char in text or "":
        code = ord(char)
        if 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF or 0xF900 <= code <= 0xFAFF:
            han += 1
        elif 0x0600 <= code <= 0x06FF or 0x0750 <= code <= 0x077F or 0xFB50 <= code <= 0xFEFF:
            arabic += 1
        elif char.isalpha() and code < 0x0250:
            latin += 1
    # One Han character carries roughly a word, so weight it against letters.
    scores = {"zh-hans": han * 3, "ur": arabic, "en": latin}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "en"
