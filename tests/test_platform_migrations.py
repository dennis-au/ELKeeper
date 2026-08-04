from __future__ import annotations

import sqlite3
import unittest

from app.modules.platform.migrations import MigrationDriftError, run_migrations, run_registered_migrations


class PlatformMigrationTests(unittest.TestCase):
    def test_migrations_run_in_declared_order(self):
        connection = sqlite3.connect(":memory:")
        calls = []
        run_migrations(connection, (lambda _: calls.append("first"), lambda _: calls.append("second")))
        self.assertEqual(calls, ["first", "second"])

    def test_registered_migrations_resume_cleanly_after_interruption(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row

        def first(db):
            db.execute("CREATE TABLE migration_fixture(value TEXT NOT NULL)")
            db.execute("INSERT INTO migration_fixture(value) VALUES('first')")

        def interrupted(db):
            db.execute("INSERT INTO migration_fixture(value) VALUES('second')")
            raise RuntimeError("interrupted")

        definitions = ((1, "first", "one", first), (2, "second", "two", interrupted))
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            run_registered_migrations(connection, definitions, table_name="fixture_migrations")
        self.assertIsNone(
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='migration_fixture'"
            ).fetchone()
        )
        self.assertIsNone(
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fixture_migrations'"
            ).fetchone()
        )

        run_registered_migrations(
            connection,
            ((1, "first", "one", first), (2, "second", "two", lambda db: db.execute("INSERT INTO migration_fixture(value) VALUES('second')"))),
            table_name="fixture_migrations",
            timestamp=lambda: "2026-08-04T00:00:00+00:00",
        )
        self.assertEqual(
            [row["value"] for row in connection.execute("SELECT value FROM migration_fixture ORDER BY value")],
            ["first", "second"],
        )
        self.assertEqual(
            [row["version"] for row in connection.execute("SELECT version FROM fixture_migrations ORDER BY version")],
            [1, 2],
        )

    def test_registered_migrations_skip_partial_ledger_and_reject_drift(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        calls = []
        run_registered_migrations(
            connection,
            ((1, "first", "one", lambda db: calls.append("first")),),
            table_name="fixture_migrations",
            timestamp=lambda: "first",
        )
        run_registered_migrations(
            connection,
            (
                (1, "first", "one", lambda db: calls.append("first-again")),
                (2, "second", "two", lambda db: calls.append("second")),
            ),
            table_name="fixture_migrations",
            timestamp=lambda: "second",
        )
        self.assertEqual(calls, ["first", "second"])
        with self.assertRaises(MigrationDriftError):
            run_registered_migrations(
                connection,
                ((1, "first", "changed", lambda db: None), (2, "second", "two", lambda db: None)),
                table_name="fixture_migrations",
            )
