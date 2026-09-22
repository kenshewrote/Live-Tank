"""Short-lived tickets that let a browser take the video straight from the tracker.

Relaying video through the public site costs a round trip per frame, so the
page streams from the tracker's own URL instead. It cannot be given the
tracker's token to do that: the token also opens the control endpoints and
never expires.

A ticket is that token, narrowed. The site signs an expiry time with it and
hands the browser the result, which the tracker accepts only for the two video
endpoints and only until it runs out. The token itself stays on the two
servers.
"""
import base64
import hmac
import time
from hashlib import sha256

DEFAULT_TTL_S = 900
# Paths a ticket is good for. Everything else still demands the real token.
VIDEO_PATHS = ("/api/video.mjpg", "/api/mask.mjpg")


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _sign(secret, expires_at):
    return _b64(hmac.new(secret.encode(), str(expires_at).encode(), sha256).digest())


def mint(secret, ttl=DEFAULT_TTL_S):
    """A ticket valid for `ttl` seconds, or "" when there is no secret to sign with."""
    if not secret:
        return ""
    expires_at = int(time.time()) + int(ttl)
    return f"{expires_at}.{_sign(secret, expires_at)}"


def verify(secret, ticket):
    if not secret or not ticket or "." not in ticket:
        return False
    expiry, _, signature = ticket.partition(".")
    try:
        expires_at = int(expiry)
    except ValueError:
        return False
    if expires_at < time.time():
        return False
    return hmac.compare_digest(_sign(secret, expires_at), signature)
