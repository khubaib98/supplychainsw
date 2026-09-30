"""
Machine translation of free text (remarks, queries, replies).

The interface (menus, statuses, document names, item names) is translated
with gettext catalogues and multilingual master data, so it works with no
translation service at all. Only what people type needs machine translation.

Providers (settings.TRANSLATION["PROVIDER"]):
  none            keep originals only (UI still fully translated)
  libretranslate  self-hosted LibreTranslate container; text never leaves your server
  anthropic       Claude API; best quality for business/logistics wording
  dummy           deterministic fake used by the test-suite

Translations are produced once, when text is written, and stored. The
original is always kept and shown on request; a translation is never the
authoritative version.
"""
import hashlib
import json
import logging
import time
import urllib.error
import urllib.request

from django.conf import settings

from core.i18n import PORTAL_LANGS, detect_language
from core.models import TextTranslation

logger = logging.getLogger(__name__)

LANG_NAMES = {"en": "English", "zh-hans": "Simplified Chinese", "ur": "Urdu"}
LIBRETRANSLATE_CODES = {"en": "en", "zh-hans": "zh", "ur": "ur"}

# After a provider failure, skip calls for a short while so pages stay fast.
_failure_until = 0.0
COOLDOWN_SECONDS = 60


class TranslationError(Exception):
    pass


def source_hash(text, source_lang):
    return hashlib.sha256(f"{source_lang}\x00{text}".encode("utf-8")).hexdigest()


class Provider:
    name = "none"

    def translate(self, text, source_lang, target_lang):
        raise NotImplementedError


class DummyProvider(Provider):
    name = "dummy"

    def translate(self, text, source_lang, target_lang):
        return f"[{target_lang}] {text}"


class LibreTranslateProvider(Provider):
    name = "libretranslate"

    def translate(self, text, source_lang, target_lang):
        config = settings.TRANSLATION
        body = {
            "q": text,
            "source": LIBRETRANSLATE_CODES[source_lang],
            "target": LIBRETRANSLATE_CODES[target_lang],
            "format": "text",
        }
        if config["LIBRETRANSLATE_API_KEY"]:
            body["api_key"] = config["LIBRETRANSLATE_API_KEY"]
        request = urllib.request.Request(
            config["LIBRETRANSLATE_URL"].rstrip("/") + "/translate",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=config["TIMEOUT"]) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise TranslationError(f"LibreTranslate request failed: {exc}") from exc
        translated = payload.get("translatedText")
        if not translated:
            raise TranslationError(f"LibreTranslate returned no text: {payload}")
        return translated


class AnthropicProvider(Provider):
    name = "anthropic"
    SYSTEM = (
        "You translate short business messages exchanged between a shipper in China and a "
        "receiver in Pakistan about export consignments (cartons, pallets, containers, "
        "invoices, packing lists, bills of lading, customs, payments). "
        "Translate the user's text faithfully into the requested language. Keep every "
        "identifier, code, number, quantity, date, unit and proper name exactly as written "
        "(for example CSG-2026-0042-CTN-017, DCJM-017, 40 pcs, B/L). Preserve negations and "
        "tone. Output only the translation, with no notes, quotes or explanations."
    )

    def translate(self, text, source_lang, target_lang):
        import anthropic

        config = settings.TRANSLATION
        client = anthropic.Anthropic(timeout=config["TIMEOUT"], max_retries=1)
        try:
            response = client.beta.messages.create(
                model=config["ANTHROPIC_MODEL"],
                max_tokens=4000,
                system=self.SYSTEM,
                output_config={"effort": config["ANTHROPIC_EFFORT"]},
                # Server-side fallback: if the model declines, the API retries on
                # Anthropic's recommended fallback model within the same call.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Translate from {LANG_NAMES[source_lang]} to {LANG_NAMES[target_lang]}:\n\n{text}"
                        ),
                    }
                ],
            )
        except anthropic.RateLimitError as exc:
            raise TranslationError("Claude API rate limit reached") from exc
        except anthropic.APIStatusError as exc:
            raise TranslationError(f"Claude API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise TranslationError(f"Claude API unreachable: {exc}") from exc
        if response.stop_reason == "refusal":
            raise TranslationError("Claude API declined to translate this text")
        parts = [block.text for block in response.content if block.type == "text"]
        translated = "".join(parts).strip()
        if not translated:
            raise TranslationError("Claude API returned no text")
        return translated


PROVIDERS = {
    "none": None,
    "dummy": DummyProvider,
    "libretranslate": LibreTranslateProvider,
    "anthropic": AnthropicProvider,
}


def get_provider():
    name = settings.TRANSLATION.get("PROVIDER", "none")
    if name not in PROVIDERS:
        raise TranslationError(f"Unknown MT_PROVIDER '{name}'")
    cls = PROVIDERS[name]
    return cls() if cls else None


def enabled():
    return get_provider() is not None


def lookup(text, source_lang, target_lang):
    """Return a stored translation or None. Never calls a provider."""
    if not text or source_lang == target_lang:
        return None
    return TextTranslation.objects.filter(source_hash=source_hash(text, source_lang), target_lang=target_lang).first()


def lookup_many(pairs, target_lang):
    """Bulk version of lookup: {(text, source_lang): TextTranslation} for rendering lists."""
    hashes = {source_hash(text, lang): (text, lang) for text, lang in pairs if text and lang != target_lang}
    found = TextTranslation.objects.filter(source_hash__in=hashes, target_lang=target_lang)
    return {hashes[row.source_hash]: row for row in found}


def translate_and_store(text, source_lang=None, targets=None):
    """
    Translate `text` into each target language that is missing and store the
    results. Returns the number of new translations. Failures are logged and
    retried later by the scheduler; they never break the user's request.
    """
    global _failure_until
    provider = get_provider()
    if provider is None or not text or not text.strip():
        return 0
    source_lang = source_lang or detect_language(text)
    digest = source_hash(text, source_lang)
    wanted = [lang for lang in (targets or PORTAL_LANGS) if lang != source_lang]
    existing = set(
        TextTranslation.objects.filter(source_hash=digest, target_lang__in=wanted).values_list("target_lang", flat=True)
    )
    created = 0
    for target in wanted:
        if target in existing:
            continue
        if time.monotonic() < _failure_until:
            break
        try:
            translated = provider.translate(text, source_lang, target)
        except TranslationError as exc:
            logger.warning("Machine translation failed (%s -> %s): %s", source_lang, target, exc)
            _failure_until = time.monotonic() + COOLDOWN_SECONDS
            break
        TextTranslation.objects.get_or_create(
            source_hash=digest,
            target_lang=target,
            defaults={"source_lang": source_lang, "text": translated, "provider": provider.name},
        )
        created += 1
    return created


def reset_cooldown():
    global _failure_until
    _failure_until = 0.0
