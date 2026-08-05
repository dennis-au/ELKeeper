from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.modules.secrets.http import build_router


class PrivateKeyDeprecationTests(unittest.TestCase):
    def setUp(self):
        self.read_remote = AsyncMock(return_value=b"never-return-this")
        app = FastAPI()
        app.include_router(
            build_router(
                catalog_provider=lambda _cluster_id: (
                    {},
                    {},
                    [
                        {
                            "id": "cluster.ca_private_key",
                            "label": "Cluster CA private key",
                            "category": "Private keys",
                            "source": "node-a",
                            "node": {"name": "node-a"},
                            "path": "/safe/path/ca.key",
                            "available": True,
                            "reveal_deprecated": True,
                        }
                    ],
                ),
                metadata_provider=lambda item: _async_value(item),
                read_remote=self.read_remote,
                verify_reauthentication=lambda _username, _password: None,
                audit_fn=lambda *_args: None,
                user_dependency=lambda: "operator",
            )
        )
        self.client = TestClient(app)

    def test_private_key_is_metadata_only_and_cannot_be_revealed_or_copied(self):
        items = self.client.get("/api/clusters/1/sensitive-items")
        self.assertEqual(items.status_code, 200)
        self.assertTrue(items.json()["items"][0]["reveal_deprecated"])

        grant = self.client.post(
            "/api/auth/reveal-grants", json={"cluster_id": 1, "password": "current"}
        ).json()["grant_token"]
        response = self.client.post(
            "/api/clusters/1/sensitive-items/cluster.ca_private_key/reveal",
            json={"grant_token": grant, "purpose": "copy"},
        )
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json()["detail"]["code"], "private_key_reveal_deprecated")
        self.read_remote.assert_not_awaited()


async def _async_value(value):
    return value


if __name__ == "__main__":
    unittest.main()
