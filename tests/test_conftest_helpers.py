"""Regression test for issue #94: `_maintenance_url` must build the
maintenance-database URL with `render_as_string(hide_password=False)`, NOT
`str(URL)` — SQLAlchemy's `str()` masks the password as `***`, so a
password-bearing `TEST_DATABASE_URL` would produce a maintenance URL pytest
could never connect with, and the entire DB-backed suite would silently skip
(reading as "no postgres configured" rather than "harness bug").

No live Postgres needed: this only exercises the string-building step, so it
must run (and pass) even when Postgres is unreachable.
"""

from .conftest import _maintenance_url


def test_maintenance_url_preserves_password():
    url = "postgresql+psycopg://testuser:s3cr3t@localhost:5432/snowline_platform_test"

    maintenance = _maintenance_url(url)

    assert "s3cr3t" in maintenance
    assert "***" not in maintenance
    assert maintenance.startswith("postgresql+psycopg://testuser:s3cr3t@")
    assert maintenance.endswith("/postgres")
