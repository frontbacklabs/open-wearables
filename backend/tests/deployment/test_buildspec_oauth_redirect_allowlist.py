"""Pin the OAuth redirect origins this deployment accepts.

The ren-web origins only work if the comma-separated OAUTH_REDIRECT_ALLOWLIST in
buildspec.yml carries them; a typo there is invisible until a user's browser lands
nowhere. These tests parse the deployed value itself -- not the matcher, whose
FRONTEND_URL and CORS_ORIGINS fallbacks would mask a missing entry.
"""

import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

REPO_ROOT = Path(__file__).resolve().parents[3]
BUILDSPEC_PATH = REPO_ROOT / "buildspec.yml"

# Anchored to the line start so a commented-out assignment cannot satisfy it.
_ALLOWLIST_RE = re.compile(r'^\s*OAUTH_REDIRECT_ALLOWLIST:\s*"([^"]+)"', re.MULTILINE)

_WEB_SCHEMES = ("http", "https")


def _deployed_allowlist() -> str:
    match = _ALLOWLIST_RE.search(BUILDSPEC_PATH.read_text(encoding="utf-8"))
    assert match, "OAUTH_REDIRECT_ALLOWLIST missing from buildspec.yml env.variables"
    return match.group(1)


def _entries(allowlist: str) -> tuple[set[str], set[str]]:
    """(web origins, exact app callbacks) the allowlist string itself contributes,
    mirroring redirect_allowlist._allowed() minus its FRONTEND_URL/CORS_ORIGINS
    fallbacks."""
    origins: set[str] = set()
    callbacks: set[str] = set()
    for entry in (e.strip() for e in allowlist.split(",") if e.strip()):
        parts = urlsplit(entry)
        if parts.scheme in _WEB_SCHEMES and parts.netloc:
            origins.add(f"{parts.scheme}://{parts.netloc}".lower())
        else:
            # Same normalization as redirect_allowlist._without_query: drop
            # query and fragment, lowercase scheme and netloc.
            callbacks.add(
                urlunsplit(
                    (parts.scheme.lower(), parts.netloc.lower(), parts.path, "", "")
                )
            )
    return origins, callbacks


def test_deployed_allowlist_covers_every_ren_callback() -> None:
    origins, callbacks = _entries(_deployed_allowlist())

    assert {
        "https://web.ren.so",
        "https://staging.web.ren.so",
        "http://localhost:3000",
    } <= origins
    assert {
        "openwearables://oauth/callback",
        "ren://integrations/callback",
        "ren-dev://integrations/callback",
        "ren-staging://integrations/callback",
    } <= callbacks
