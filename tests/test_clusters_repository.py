from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from app.modules.clusters import ClusterRepository
from app.modules.platform.db import connect


class ClusterRepositoryTests(unittest.TestCase):
    def test_cluster_ids_are_ordered_by_name(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "clusters.db"
            with connect(database) as connection:
                connection.execute("CREATE TABLE clusters(id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
                connection.executemany("INSERT INTO clusters(id,name) VALUES (?,?)", [(1, "zeta"), (2, "alpha")])
            repository = ClusterRepository(lambda: connect(database))
            self.assertEqual(repository.ids(), [2, 1])
            self.assertTrue(repository.exists(1))
            self.assertFalse(repository.exists(9))

    def test_membership_write_supports_legacy_advertised_address_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "memberships.db"
            with connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE memberships (
                      cluster_id INTEGER NOT NULL,
                      node_id INTEGER NOT NULL,
                      advertised_address TEXT,
                      network_mode TEXT NOT NULL,
                      data_interface TEXT NOT NULL,
                      data_address TEXT NOT NULL,
                      user_interface TEXT NOT NULL,
                      user_address TEXT NOT NULL,
                      PRIMARY KEY(cluster_id,node_id)
                    );
                    """
                )
                repository = ClusterRepository.from_connection(connection)
                membership = SimpleNamespace(
                    node_id=3,
                    network_mode="shared",
                    data_interface="ens18",
                    data_address="192.0.2.10",
                    user_interface="ens18",
                    user_address="192.0.2.10",
                )
                repository.insert_membership_in_connection(connection, 4, membership)
                self.assertTrue(repository.update_membership_in_connection(connection, 4, 3, membership))
                row = connection.execute("SELECT * FROM memberships").fetchone()
                self.assertEqual(row["advertised_address"], "192.0.2.10")
                repository.delete_membership_in_connection(connection, 4, 3)
                self.assertIsNone(connection.execute("SELECT * FROM memberships").fetchone())
