"""Single source of truth for the console's build version.

Bumped on every shipped change so the operator can tell builds apart — it is shown
bottom-left in the UI (via the ``app_version`` Jinja global and SPA version
endpoint) and reported as the FastAPI app version. See CHANGELOG.md for what each
version delivered.

Scheme: ``0.1.<iteration>`` while pre-1.0 — bump the patch on each shipped change.
"""

__version__ = "0.1.233"
