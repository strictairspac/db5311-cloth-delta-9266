"""Test bootstrap: derive application DB settings from GB5311_TEST_DATABASE_URL.

The application modules read DB_HOST/DB_PORT/... at import time via
src.config.settings, so the environment has to be fixed before any src import.
conftest.py is imported by pytest before the test modules are collected.
"""

import os

from sqlalchemy.engine.url import make_url

TEST_URL = os.getenv('GB5311_TEST_DATABASE_URL')
if TEST_URL:
    parsed = make_url(TEST_URL)
    os.environ.setdefault('DB_DRIVER', parsed.drivername)
    os.environ.setdefault('DB_HOST', parsed.host or '127.0.0.1')
    os.environ.setdefault('DB_PORT', str(parsed.port or 5432))
    os.environ.setdefault('DB_NAME', parsed.database or 'workflow')
    os.environ.setdefault('DB_USER', parsed.username or 'app')
    if parsed.password:
        os.environ.setdefault('DB_PASSWORD', parsed.password)
    os.environ.setdefault('AUTH_KEY', 'test-key')
    # No Redis in the test environment; call sites already tolerate failures.
    os.environ.setdefault('REDIS_URL', 'redis://127.0.0.1:6399/0')
