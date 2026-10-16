# ============================================================
# GENERATION TICKET (SIGNED SQL PROVENANCE)
# ============================================================
# The SQL pipeline is two steps: /api/ask GENERATES a statement and
# /api/run EXECUTES one. The two arrive in different requests, and the
# only thing tying them together is whatever the browser sends back.
#
# That makes provenance a trust problem. If /api/run simply believed the
# "provider" and "model" fields a client posts, anyone could run
# hand-written SQL and label it as a model's output. Equally, a manual
# statement must not be reported as AI-generated.
#
# This module removes the guesswork by having the server sign what it
# generated:
#
#   * after a successful generation, /api/ask issues a ticket binding
#     (provider, model, sha256(sql)) under an HMAC key derived from the
#     server secret;
#   * /api/run verifies the ticket and compares the digest of the SQL it
#     was handed with the digest inside it. That yields exactly three
#     truthful origins:
#
#       "generated" - the SQL is byte-for-byte what a model produced;
#       "edited"    - a valid ticket exists but the SQL differs, so the
#                     user changed the model's statement;
#       "manual"    - no valid ticket, so the statement is the user's
#                     own (or the ticket was forged and rejected).
#
# THE HONEST LIMITS, STATED PLAINLY
#   * The ticket is signed, not encrypted. It carries no secret, only
#     the provider/model the client was already shown and a digest of
#     SQL it already holds, so there is nothing to hide. Its only job is
#     to be unforgeable.
#   * "generated" means a model produced the statement; it says nothing
#     about whether the statement is correct. That line is drawn by
#     app/correctness.py, not here.
#   * The ticket is deterministic and does not expire: it is provenance,
#     not a capability, and it grants no access and no execution. A
#     replayed ticket can only re-label SQL that a model genuinely
#     produced, which is the true answer anyway.
#   * Editing whitespace can move "generated" to "edited". Erring toward
#     "edited" under-claims, which is the safe direction.
# ============================================================

import base64
import hashlib
import hmac
import json

from app.auth import sign_payload
from app.access import signing_secret

_TICKET_VERSION = "gt1"

# Domain separation: the ticket key is a distinct derivation of the
# visitor-cookie signing key, so a signature minted for a cookie can
# never be replayed as a ticket and settling one does not settle the
# other.
_TICKET_LABEL = "tiix-generation-ticket-v1"

GENERATED = "generated"
EDITED = "edited"
MANUAL = "manual"


def _ticket_key():

    return sign_payload(_TICKET_LABEL, signing_secret())


def _sql_digest(sql):

    text = sql if isinstance(sql, str) else ""

    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def _encode(provider, model, sql):

    payload = json.dumps(
        {
            "p": provider if isinstance(provider, str) else "",
            "m": model if isinstance(model, str) else "",
            "h": _sql_digest(sql),
        },
        separators=(",", ":"),
        sort_keys=True,
    )

    return base64.urlsafe_b64encode(
        payload.encode("utf-8")
    ).decode("ascii").rstrip("=")


def issue(provider, model, sql):
    """Sign what a generation produced, or return None.

    Deterministic: the same (provider, model, sql) always yields the same
    ticket, so a byte-identical generation stays byte-identical. Returns
    None when the server secret is unavailable, in which case /api/run
    simply treats later statements as manual rather than failing.
    """

    try:

        body = _encode(provider, model, sql)

        signature = sign_payload(
            f"{_TICKET_VERSION}.{body}", _ticket_key()
        )

        return f"{_TICKET_VERSION}.{body}.{signature}"

    except Exception:

        return None


def verify(token):
    """Return the signed claims, or None when the ticket is unusable.

    None means "not one of ours": a forged, truncated or foreign ticket
    is treated exactly like no ticket at all.
    """

    if not token or not isinstance(token, str):

        return None

    parts = token.split(".")

    if len(parts) != 3:

        return None

    version, body, signature = parts

    if version != _TICKET_VERSION or not body or not signature:

        return None

    try:

        expected = sign_payload(
            f"{version}.{body}", _ticket_key()
        )

    except Exception:

        return None

    if not hmac.compare_digest(signature, expected):

        return None

    try:

        padded = body + "=" * (-len(body) % 4)

        payload = json.loads(
            base64.urlsafe_b64decode(padded).decode("utf-8")
        )

    except Exception:

        return None

    if not isinstance(payload, dict):

        return None

    provider = payload.get("p")
    model = payload.get("m")
    digest = payload.get("h")

    if not isinstance(provider, str):
        return None

    if not isinstance(model, str):
        return None

    if not isinstance(digest, str) or len(digest) != 64:
        return None

    return {"provider": provider, "model": model, "sql_digest": digest}


def classify(token, sql):
    """The truthful origin of a statement, plus the model that wrote its base.

    Returns (origin, provider, model):
      * ("generated", p, m) when a valid ticket's digest matches the SQL;
      * ("edited",    p, m) when a valid ticket exists but the SQL differs;
      * ("manual",    None, None) when there is no valid ticket.
    """

    claims = verify(token)

    if claims is None:

        return MANUAL, None, None

    if claims["sql_digest"] == _sql_digest(sql):

        return GENERATED, claims["provider"], claims["model"]

    return EDITED, claims["provider"], claims["model"]
