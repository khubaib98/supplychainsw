"""
Identifier scheme.

Rule: an ID may only contain facts that can never change ("birth facts"):
the consignment it was created in, what kind of thing it is, and a running
number. It must never contain *where the thing currently is*: that is a
relationship stored in the database and updated by scan events, so moving
an item between cartons never makes its label wrong.

    Consignment   CSG-2026-0042
    Handling unit CSG-2026-0042-CTN-017      (CNT container, PLT pallet, CRT crate, CTN carton, BOX inner box)
    Item          CSG-2026-0042-DCJM-017     (item-type code + number within this consignment)
"""
import re
import unicodedata
from urllib.parse import unquote, urlparse

from django.conf import settings
from django.db import IntegrityError, transaction

from core.models import Sequence

CONSIGNMENT_RE = re.compile(r"^(?P<prefix>[A-Z]{2,5})-(?P<year>\d{4})-(?P<num>\d{4,})$")
CHILD_RE = re.compile(
    r"^(?P<consignment>[A-Z]{2,5}-\d{4}-\d{4,})-(?P<code>[A-Z][A-Z0-9]{1,7})-(?P<num>\d{3,})$"
)


def reserve(key, count=1):
    """Atomically reserve `count` consecutive numbers for `key`; returns the first one."""
    if count < 1:
        raise ValueError("count must be positive")
    with transaction.atomic():
        try:
            with transaction.atomic():
                Sequence.objects.create(key=key)
        except IntegrityError:
            pass
        seq = Sequence.objects.select_for_update().get(key=key)
        first = seq.value + 1
        seq.value += count
        seq.save(update_fields=["value"])
        return first


def bump_to(key, value):
    """Make sure the counter for `key` is at least `value` (after importing explicit numbers)."""
    with transaction.atomic():
        try:
            with transaction.atomic():
                Sequence.objects.create(key=key)
        except IntegrityError:
            pass
        seq = Sequence.objects.select_for_update().get(key=key)
        if seq.value < value:
            seq.value = value
            seq.save(update_fields=["value"])


def consignment_code(year, number):
    prefix = settings.PORTAL["CONSIGNMENT_PREFIX"]
    return f"{prefix}-{year:04d}-{number:04d}"


def next_consignment_code(year):
    prefix = settings.PORTAL["CONSIGNMENT_PREFIX"]
    return consignment_code(year, reserve(f"{prefix}-{year:04d}"))


def child_code(consignment_code_, code, number):
    """Unit or item ID: <consignment>-<CTN|PLT|…|item code>-<nnn>."""
    return f"{consignment_code_}-{code}-{number:03d}"


def short_code(full_code):
    """CSG-2026-0042-CTN-017 -> CTN-017 (for display inside a consignment)."""
    match = CHILD_RE.match(full_code or "")
    if not match:
        return full_code
    return f"{match['code']}-{match['num']}"


def normalize_scan(raw):
    """
    Clean up whatever a scanner, camera or person typed:
    - NFKC folds full-width characters that a Chinese IME produces (ＣＳＧ－２０２６ -> CSG-2026)
    - a URL payload is reduced to its last path segment
    - whitespace is removed and letters upper-cased
    """
    if raw is None:
        return ""
    text = unicodedata.normalize("NFKC", str(raw)).strip()
    if "://" in text:
        path = urlparse(text).path.rstrip("/")
        text = unquote(path.rsplit("/", 1)[-1]) if path else text
    text = re.sub(r"\s+", "", text)
    # Different hyphen-like characters (en dash, minus sign…) become ASCII hyphen.
    text = re.sub(r"[‐-―−ーｰ]", "-", text)
    return text.upper()


ISO6346_RE = re.compile(r"^[A-Z]{3}[UJZR]\d{7}$")


def iso6346_valid(container_no):
    """Check the check digit of a shipping-container number such as MSKU1234565 (ISO 6346)."""
    code = re.sub(r"[\s-]", "", container_no or "").upper()
    if not ISO6346_RE.match(code):
        return False
    values = {}
    number = 10
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if number % 11 == 0:
            number += 1
        values[letter] = number
        number += 1
    total = sum((values[ch] if ch.isalpha() else int(ch)) * (2**index) for index, ch in enumerate(code[:10]))
    return (total % 11) % 10 == int(code[10])


def classify(code):
    """Return ('consignment'|'child'|None, match) for a normalised code."""
    if CONSIGNMENT_RE.match(code):
        return "consignment", CONSIGNMENT_RE.match(code)
    match = CHILD_RE.match(code)
    if match:
        return "child", match
    return None, None
