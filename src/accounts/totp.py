"""RFC 6238 time-based one-time passwords (works with any authenticator app)."""
import base64
import hashlib
import hmac
import os
import struct
import time
from urllib.parse import quote

STEP_SECONDS = 30
DIGITS = 6


def new_secret():
    return base64.b32encode(os.urandom(20)).decode("ascii").rstrip("=")


def _key(secret):
    padded = secret.upper() + "=" * (-len(secret) % 8)
    return base64.b32decode(padded)


def code_at(secret, step, digits=DIGITS):
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(number % (10**digits)).zfill(digits)


def current_step(now=None):
    return int((time.time() if now is None else now) // STEP_SECONDS)


def verify(secret, code, last_used_step=0, window=1, now=None):
    """
    Return the matching time step, or None. Steps at or before
    `last_used_step` are rejected so a code cannot be replayed.
    """
    if not secret:
        return None
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != DIGITS:
        return None
    step = current_step(now)
    for candidate in range(step - window, step + window + 1):
        if candidate > last_used_step and hmac.compare_digest(code_at(secret, candidate), code):
            return candidate
    return None


def provisioning_uri(secret, account, issuer):
    label = quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&digits={DIGITS}&period={STEP_SECONDS}"
