"""Error text that is safe to print. Request exceptions often include the URL."""

import os
import re
import sys

_SECRET = re.compile(
    r'(?i)((?:api[_-]?key|apikey|token|authorization)=)[^&\s\'"]+'
)


def public_error(exc):
    """Exception text with query-string secrets replaced."""
    return _SECRET.sub(r'\1***', str(exc))


def require_env(*names):
    """Exit if a required variable did not load from the environment or .env."""
    missing = [name for name in names if not os.getenv(name)]
    if not missing:
        return
    print("Missing environment variables: " + ", ".join(missing))
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        print(f"No .env file at {env_path}. Copy .env.example to .env and add the keys.")
    else:
        print(f"Add them to {env_path}. Names are listed in .env.example.")
    sys.exit(1)
