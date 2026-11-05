"""Settings shared by the agent and the ticker.

Everything can be set through environment variables so the same code runs on
the laptop, in a container and on the cloud VM without changes.
"""

import os
import sys

NAMESPACE = "agntcy"
GROUP = "demo"
AGENT_NAME = "home_agent"

DEFAULT_SLIM_URL = "http://localhost:46357"


def slim_url() -> str:
    return os.environ.get("SLIM_URL", DEFAULT_SLIM_URL)


def shared_secret() -> str:
    """Return the shared secret, or exit with a hint if it is missing.

    There is deliberately no fallback: the demo node is reachable from the
    internet, so a publicly known placeholder secret would let anyone join.
    """
    secret = os.environ.get("SLIM_SHARED_SECRET", "")
    if len(secret) < 32:
        sys.exit(
            "SLIM_SHARED_SECRET must be set to at least 32 characters "
            "(run `task secret` to generate one into .env)."
        )
    return secret
