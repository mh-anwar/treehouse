"""Cloudflare Access JWT verification (RS256), stdlib only.

With PUBLIC_MODE=cloudflare every request reaching sharedbox-gate went through Cloudflare Access, which adds
a signed JWT (the Cf-Access-Jwt-Assertion header, and the CF_Authorization cookie; CLI clients send it
themselves as cf-access-token). Checking it here is the
second lock: without it, anyone who can reach 127.0.0.1:GATE_PORT (every local account) could skip Access.

  - RS256 only. The header's alg must be exactly "RS256": no "none", no HS256 (alg confusion), no others.
  - The key comes from the team's JWKS (https://<team>.cloudflareaccess.com/cdn-cgi/access/certs), by kid.
    Keys are cached; an unknown kid refreshes the set at most once a minute, a stale set (6 h) on next use.
  - The signature is checked with pow() and compared against the ONE valid PKCS#1 v1.5 encoding of the
    SHA-256 DigestInfo (00 01 FF..FF 00 || DigestInfo || hash), byte for byte, in constant time. Nothing is
    parsed out of the decrypted block, so the classic loose-parser forgeries (Bleichenbacher '06: short
    padding, garbage after the hash, odd DigestInfo lengths) have nothing to exploit.
  - aud must contain CF_ACCESS_AUD, iss must be https://<team domain>, exp/nbf/iat are checked with 60 s of
    skew, and the token must carry an email (that's the identity).

Used by sharedbox-gate (every request) and by root (sharedbox-admin re-verifies the token behind every
signup or portal action).
"""

import base64
import hashlib
import hmac
import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request

SHA256_DIGESTINFO = bytes.fromhex("3031300d060960864801650304020105000420")
B64URL = re.compile(r"[A-Za-z0-9_-]*")
EMAIL_RE = re.compile(r"[^@\s]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")
MAX_TOKEN = 16384
SKEW = 60                 # seconds of clock skew allowed on exp / nbf / iat
MIN_BITS = 2048
REFRESH_MIN_INTERVAL = 60  # unknown kid: refetch the JWKS at most this often
MAX_KEY_AGE = 6 * 3600     # refetch a set this old on next use (Cloudflare rotates keys every ~6 weeks)
FETCH_TIMEOUT = 5
FETCH_LIMIT = 256 << 10


class InvalidToken(Exception):
    pass


def certs_url(team_domain):
    """Where the team's signing keys live. SHAREDBOX_CF_CERTS_URL overrides it (tests only)."""
    return os.environ.get("SHAREDBOX_CF_CERTS_URL") or f"https://{team_domain}/cdn-cgi/access/certs"


def b64url_decode(s):
    """Strict base64url (no padding, canonical): anything else is an invalid token."""
    if not isinstance(s, str) or not B64URL.fullmatch(s) or len(s) % 4 == 1:
        raise InvalidToken("bad base64url")
    raw = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode() != s:
        raise InvalidToken("non-canonical base64url")
    return raw


def b64url_encode(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _json_obj(raw):
    def no_dupes(pairs):
        d = {}
        for k, v in pairs:
            if k in d:
                raise InvalidToken("duplicate JSON key")
            d[k] = v
        return d
    try:
        v = json.loads(raw.decode("utf-8"), object_pairs_hook=no_dupes)
    except (UnicodeDecodeError, ValueError):
        raise InvalidToken("bad JSON") from None
    if not isinstance(v, dict):
        raise InvalidToken("not a JSON object")
    return v


def rsa_pkcs1v15_sha256_verify(n, e, message, sig):
    """True only if `sig` is a valid RSASSA-PKCS1-v1_5 SHA-256 signature of `message` (RFC 8017 8.2.2)."""
    k = (n.bit_length() + 7) // 8
    if len(sig) != k:
        return False
    s = int.from_bytes(sig, "big")
    if not 0 < s < n:
        return False
    em = pow(s, e, n).to_bytes(k, "big")
    t = SHA256_DIGESTINFO + hashlib.sha256(message).digest()
    if k < len(t) + 11:
        return False
    expected = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return hmac.compare_digest(em, expected)


def parse_jwks(doc):
    """{kid: (n, e)} for the usable RSA signing keys in a JWKS document. Others are skipped."""
    keys = {}
    for j in (doc.get("keys") if isinstance(doc, dict) else None) or []:
        if not isinstance(j, dict) or j.get("kty") != "RSA":
            continue
        if j.get("alg", "RS256") != "RS256" or j.get("use", "sig") != "sig":
            continue
        kid = j.get("kid")
        if not isinstance(kid, str) or not kid or len(kid) > 200:
            continue
        try:
            n = int.from_bytes(b64url_decode(j.get("n")), "big")
            e = int.from_bytes(b64url_decode(j.get("e")), "big")
        except InvalidToken:
            continue
        if n.bit_length() < MIN_BITS or n % 2 == 0 or e < 3 or e % 2 == 0 or e.bit_length() > 32:
            continue
        keys[kid] = (n, e)
    return keys


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def fetch_json(url, timeout=FETCH_TIMEOUT):
    if not url.startswith("https://") and not os.environ.get("SHAREDBOX_CF_CERTS_URL"):
        raise ValueError("JWKS URL must be https")
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "sharedbox-gate"})
    with _opener.open(req, timeout=timeout) as r:
        if r.status != 200:
            raise ValueError(f"HTTP {r.status}")
        data = r.read(FETCH_LIMIT + 1)
    if len(data) > FETCH_LIMIT:
        raise ValueError("JWKS too large")
    return json.loads(data)


class KeySet:
    """The team's signing keys, cached. Thread-safe. `cache_file` (optional) seeds the cache at start and
    is rewritten after each successful fetch when writable (root keeps it; the gate only reads it)."""

    def __init__(self, url, fetch=fetch_json, cache_file=None, clock=time.time):
        self.url, self.fetch, self.cache_file, self.clock = url, fetch, cache_file, clock
        self._keys, self._fetched, self._tried = {}, 0.0, -1e18
        self._lock = threading.Lock()
        self.fetches = 0
        self.last_error = ""
        if cache_file:
            try:
                with open(cache_file) as f:
                    c = json.load(f)
                if c.get("url") == url:
                    self._keys = parse_jwks(c.get("jwks"))
                    self._fetched = float(c.get("fetched", 0))
            except (OSError, ValueError, TypeError, AttributeError):
                pass

    def refresh(self):
        self.fetches += 1
        try:
            doc = self.fetch(self.url)
        except (OSError, ValueError, urllib.error.URLError) as ex:
            self.last_error = f"{type(ex).__name__}: {ex}"[:200]
            return False
        keys = parse_jwks(doc)
        if not keys:
            self.last_error = "no usable RSA keys in the JWKS"
            return False
        with self._lock:
            self._keys, self._fetched, self.last_error = keys, self.clock(), ""
        if self.cache_file:
            self._save(doc)
        return True

    def _save(self, doc):
        try:
            d = os.path.dirname(self.cache_file)
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-certs-")
            with os.fdopen(fd, "w") as f:
                json.dump({"url": self.url, "fetched": self.clock(), "jwks": doc}, f)
                os.fchmod(f.fileno(), 0o640)
            os.replace(tmp, self.cache_file)
        except OSError:
            pass  # read-only for this process (the gate): fine

    def get(self, kid):
        with self._lock:
            key = self._keys.get(kid)
            now = self.clock()
            if key and now - self._fetched < MAX_KEY_AGE:
                return key
            if now - self._tried < REFRESH_MIN_INTERVAL:
                return key  # refreshed (or tried) less than a minute ago: don't hammer Cloudflare
            self._tried = now
        refreshed = self.refresh()
        with self._lock:
            # a retired key is gone once a fetch worked; if the fetch failed, a stale set stays usable
            return self._keys.get(kid) if refreshed else key

    def count(self):
        return len(self._keys)


class Verifier:
    def __init__(self, team_domain, aud, keyset=None, cache_file=None, clock=time.time, skew=SKEW):
        team_domain = (team_domain or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", team_domain):
            raise ValueError("CF_TEAM_DOMAIN must look like <team>.cloudflareaccess.com")
        if not aud or not re.fullmatch(r"[A-Za-z0-9]{16,128}", aud):
            raise ValueError("CF_ACCESS_AUD must be the Access application's AUD tag")
        self.iss, self.aud, self.clock, self.skew = "https://" + team_domain, aud, clock, skew
        self.keys = keyset or KeySet(certs_url(team_domain), cache_file=cache_file, clock=clock)

    def verify(self, token):
        """The token's claims (with "email" lowercased) if it is valid for this app, else InvalidToken."""
        if not isinstance(token, str) or not token or len(token) > MAX_TOKEN or token.count(".") != 2:
            raise InvalidToken("malformed token")
        h64, p64, s64 = token.split(".")
        header = _json_obj(b64url_decode(h64))
        if header.get("alg") != "RS256":
            raise InvalidToken("alg must be RS256")
        if "crit" in header:
            raise InvalidToken("unsupported critical header")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid or len(kid) > 200:
            raise InvalidToken("missing kid")
        sig = b64url_decode(s64)
        key = self.keys.get(kid)
        if not key:
            raise InvalidToken("unknown signing key")
        if not rsa_pkcs1v15_sha256_verify(key[0], key[1], f"{h64}.{p64}".encode("ascii"), sig):
            raise InvalidToken("bad signature")
        claims = _json_obj(b64url_decode(p64))
        aud = claims.get("aud")
        auds = [aud] if isinstance(aud, str) else aud if isinstance(aud, list) else []
        if not any(isinstance(a, str) and hmac.compare_digest(a.encode(), self.aud.encode()) for a in auds):
            raise InvalidToken("wrong audience")
        if claims.get("iss") != self.iss:
            raise InvalidToken("wrong issuer")
        now = self.clock()

        def num(name, required=False):
            v = claims.get(name)
            if v is None and not required:
                return None
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise InvalidToken(f"bad {name}")
            return v
        if now >= num("exp", True) + self.skew:
            raise InvalidToken("expired")
        nbf, iat = num("nbf"), num("iat")
        if nbf is not None and nbf > now + self.skew:
            raise InvalidToken("not yet valid")
        if iat is not None and iat > now + self.skew:
            raise InvalidToken("issued in the future")
        email = claims.get("email")
        if not isinstance(email, str) or len(email) > 254 or not EMAIL_RE.fullmatch(email):
            raise InvalidToken("no email in token")
        return dict(claims, email=email.strip().lower())


def token_from_headers(headers):
    """The Access JWT from (name, value) header pairs: Cf-Access-Jwt-Assertion, else cf-access-token (what
    `cloudflared access curl` / a CLI agent sends: the same app token, in case the edge doesn't turn it into
    the assertion header), else the CF_Authorization cookie. When the assertion header is there it wins
    (cloudflared always sets it), even if the others differ. Every source is verified the same way."""
    for name in ("cf-access-jwt-assertion", "cf-access-token"):
        hdr = [v for n, v in headers if n.lower() == name]
        if hdr:
            return hdr[0].strip() if len(hdr) == 1 else None  # two different headers: refuse
    for n, v in headers:
        if n.lower() == "cookie":
            for part in v.split(";"):
                name, _, val = part.strip().partition("=")
                if name == "CF_Authorization" and val.strip():
                    return val.strip()
    return None
