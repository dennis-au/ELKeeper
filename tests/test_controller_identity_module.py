import base64
from pathlib import Path
import tempfile
import unittest

from fastapi import HTTPException

from app.modules.controller_identity import ControllerIdentityService, public_key_fingerprint
from app.modules.platform.db import connect


class ControllerIdentityTests(unittest.TestCase):
    def test_fingerprint_is_stable_and_does_not_return_key_material(self):
        key = "ssh-ed25519 " + base64.b64encode(b"test-public-key").decode() + " comment"
        fingerprint = public_key_fingerprint(key)
        self.assertTrue(fingerprint.startswith("SHA256:"))
        self.assertNotIn("test-public-key", fingerprint)
        self.assertEqual(fingerprint, public_key_fingerprint(key))

    def test_staged_key_uses_host_contract_and_activates_after_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "identity.db"
            with connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE controller_ssh_keys (
                      id INTEGER PRIMARY KEY,
                      key_id TEXT UNIQUE NOT NULL,
                      algorithm TEXT NOT NULL,
                      public_key TEXT NOT NULL,
                      private_key_encrypted TEXT NOT NULL,
                      source TEXT NOT NULL,
                      state TEXT NOT NULL
                    );
                    CREATE TABLE nodes (
                      id INTEGER PRIMARY KEY,
                      name TEXT UNIQUE NOT NULL,
                      address TEXT NOT NULL,
                      ssh_port INTEGER NOT NULL,
                      ssh_user TEXT NOT NULL,
                      enabled INTEGER NOT NULL,
                      candidate_key_id TEXT NOT NULL DEFAULT '',
                      ssh_key_id TEXT NOT NULL DEFAULT '',
                      ssh_auth_state TEXT NOT NULL DEFAULT 'legacy',
                      legacy_known_hosts_disabled INTEGER NOT NULL DEFAULT 0
                    );
                    INSERT INTO nodes(name,address,ssh_port,ssh_user,enabled)
                    VALUES ('node-1','192.0.2.10',22,'root',1);
                    """
                )

            service = ControllerIdentityService(lambda: connect(database), seal_secret=lambda value: "sealed:" + value)
            row, retired = service.stage(
                private_value="private-value",
                public_key="ssh-ed25519 AAAA test",
                key_id="SHA256:candidate",
                algorithm="ed25519",
                source="generated",
            )
            self.assertEqual(row["state"], "candidate")
            self.assertEqual(retired, [])
            with self.assertRaisesRegex(HTTPException, "Install and verify"):
                service.candidate_activation()

            with connect(database) as connection:
                connection.execute("UPDATE nodes SET candidate_key_id='SHA256:candidate' WHERE id=1")
            active, candidate = service.candidate_activation()
            self.assertIsNone(active)
            service.activate(active, candidate)
            with connect(database) as connection:
                key = connection.execute("SELECT state,private_key_encrypted FROM controller_ssh_keys").fetchone()
                node = connection.execute("SELECT ssh_key_id,candidate_key_id,ssh_auth_state FROM nodes").fetchone()
            self.assertEqual((key["state"], key["private_key_encrypted"]), ("active", "sealed:private-value"))
            self.assertEqual((node["ssh_key_id"], node["candidate_key_id"], node["ssh_auth_state"]),
                             ("SHA256:candidate", "", "controller_key"))
