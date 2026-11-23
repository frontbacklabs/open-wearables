"""Pin the OAuth redirect origins this deployment accepts.

The ren-web origins only work if the comma-separated OAUTH_REDIRECT_ALLOWLIST in
buildspec.yml carries them; a typo there is invisible until a user's browser lands
nowhere. These tests parse the deployed value and run it through the same matcher
the authorize endpoint uses.
"""

import re
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from app.utils.redirect_allowlist import is_allowed_redirect

REPO_ROOT = Path(__file__).resolve().parents[3]
BUILDSPEC_PATH = REPO_ROOT / "buildspec.yml"

_ALLOWLIST_RE = re.compile(r'OAUTH_REDIRECT_ALLOWLIST:\s*"([^"]+)"')


def _deployed_allowlist() -> str:
    match = _ALLOWLIST_RE.search(BUILDSPEC_PATH.read_text(encoding="utf-8"))
    assert match, "OAUTH_REDIRECT_ALLOWLIST missing from buildspec.yml"
    return match.group(1)


def test_deployed_allowlist_covers_every_ren_callback() -> None:
    with patch.object(settings, "oauth_redirect_allowlist", _deployed_allowlist()):
        for uri in (
            "openwearables://oauth/callback",
            "ren://integrations/callback",
            "ren-dev://integrations/callback",
            "ren-staging://integrations/callback",
            "https://web.ren.so/integrations/callback",
            "https://staging.web.ren.so/integrations/callback",
            "http://localhost:3000/integrations/callback",
        ):
            assert is_allowed_redirect(uri), uri

        assert not is_allowed_redirect("https://evil.example/callback")
