from __future__ import annotations

from contextlib import contextmanager
import asyncio
from datetime import datetime, timedelta, timezone
import ipaddress
import sqlite3
import tempfile
import unittest

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from app.modules.certificates import (
    CertificateLifecycleService,
    inspect_certificate_chain,
    install_certificate_schema,
)


def certificate_authority() -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ELKeeper test CA")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, certificate


def leaf_certificate(
    authority_key: rsa.RSAPrivateKey,
    authority: x509.Certificate,
    *,
    include_client_auth: bool = True,
) -> x509.Certificate:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    usages = [ExtendedKeyUsageOID.SERVER_AUTH]
    if include_client_auth:
        usages.append(ExtendedKeyUsageOID.CLIENT_AUTH)
    return (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "node-a")]))
        .issuer_name(authority.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=90))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("node-a"), x509.IPAddress(ipaddress.ip_address("192.0.2.10"))]
            ),
            critical=False,
        )
        .add_extension(x509.ExtendedKeyUsage(usages), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(authority_key, hashes.SHA256())
    )


def pem(certificate: x509.Certificate) -> bytes:
    return certificate.public_bytes(serialization.Encoding.PEM)


class CertificateObservationTests(unittest.TestCase):
    def setUp(self):
        self.database = tempfile.NamedTemporaryFile(suffix=".db")
        with self.db() as connection:
            connection.execute(
                "CREATE TABLE clusters (id INTEGER PRIMARY KEY, desired_version TEXT NOT NULL)"
            )
            connection.execute("INSERT INTO clusters(id,desired_version) VALUES(7,'8.19.0')")
            install_certificate_schema(connection)
        self.cluster = {
            "id": 7,
            "slug": "lab-a",
            "desired_version": "8.19.0",
            "assignments": [{"id": 11, "node_id": 3, "node_name": "node-a", "role": "master"}],
        }
        self.service = CertificateLifecycleService(
            db_factory=self.db,
            cluster_provider=lambda cluster_id: self.cluster if cluster_id == 7 else None,
        )
        self.authority_key, self.authority = certificate_authority()

    def tearDown(self):
        self.database.close()

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.database.name)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def test_transport_inspection_validates_chain_sans_and_both_transport_ekus_without_persisting_pem(self):
        leaf = leaf_certificate(self.authority_key, self.authority)

        inspection = inspect_certificate_chain(
            pem(leaf),
            (pem(self.authority),),
            purpose="elasticsearch_transport",
            expected_dns=("node-a",),
            expected_ips=("192.0.2.10",),
        )

        self.assertEqual(inspection["validation"], {
            "chain": "verified",
            "eku": "valid",
            "health": "healthy",
            "san": "matched",
            "validity": "valid",
        })
        self.assertIn("serverAuth", inspection["metadata"]["extended_key_usage"])
        self.assertIn("clientAuth", inspection["metadata"]["extended_key_usage"])
        self.assertNotIn("BEGIN CERTIFICATE", str(inspection))

    def test_transport_inspection_fails_when_a_present_eku_omits_client_auth(self):
        leaf = leaf_certificate(self.authority_key, self.authority, include_client_auth=False)

        inspection = inspect_certificate_chain(
            pem(leaf),
            (pem(self.authority),),
            purpose="elasticsearch_transport",
        )

        self.assertEqual(inspection["validation"]["eku"], "missing_required")
        self.assertEqual(inspection["validation"]["health"], "degraded")

    def test_service_persists_only_public_generation_and_observation_evidence(self):
        inventory = self.service.list_assets(7)
        asset = next(item for item in inventory["items"] if item["purpose"] == "elasticsearch_transport")
        leaf = leaf_certificate(self.authority_key, self.authority)

        result = self.service.record_inspection(
            asset["id"],
            certificate_pem=pem(leaf),
            chain_pems=(pem(self.authority),),
            source="remote_file",
        )
        detail = self.service.asset_detail(asset["id"])

        self.assertEqual(result["asset"]["health"], "healthy")
        self.assertEqual(len(detail["generations"]), 1)
        self.assertEqual(len(detail["observations"]), 1)
        self.assertEqual(detail["observations"][0]["validation"]["health"], "healthy")
        self.assertNotIn("private_key", str(detail))
        self.assertNotIn("BEGIN CERTIFICATE", str(detail))

    def test_refresh_collects_remote_public_metadata_and_records_generic_failures(self):
        leaf = leaf_certificate(self.authority_key, self.authority)
        reads: list[tuple[int, str]] = []

        async def read_certificate(node, path):
            reads.append((node["id"], path))
            if path.endswith("/ca/ca.crt"):
                return pem(self.authority)
            if path.endswith("node.crt"):
                return pem(leaf)
            raise RuntimeError("unexpected path")

        service = CertificateLifecycleService(
            db_factory=self.db,
            cluster_provider=lambda cluster_id: self.cluster if cluster_id == 7 else None,
            node_provider=lambda node_id: {"id": node_id, "address": "192.0.2.10"},
            remote_file_reader=read_certificate,
            completed_run=lambda *_args: 17,
        )

        refreshed = asyncio.run(service.refresh(7, username="operator"))

        self.assertEqual(refreshed["run_id"], 17)
        self.assertEqual(refreshed["mode"], "remote_metadata")
        self.assertGreater(refreshed["summary"]["collected"], 0)
        self.assertEqual(refreshed["summary"]["failed"], 0)
        self.assertTrue(reads)
        self.assertTrue(all(item["last_observed_at"] for item in refreshed["inventory"]["items"]))

    def test_refresh_reads_shared_ca_from_the_master_when_another_assignment_comes_first(self):
        leaf = leaf_certificate(self.authority_key, self.authority)
        self.cluster["assignments"] = [
            {"id": 12, "node_id": 4, "node_name": "node-b", "role": "kibana"},
            {"id": 11, "node_id": 3, "node_name": "node-a", "role": "master"},
        ]
        reads: list[tuple[int, str]] = []

        async def read_certificate(node, path):
            reads.append((node["id"], path))
            if path.endswith("/ca/ca.crt"):
                return pem(self.authority)
            if path.endswith("node.crt"):
                return pem(leaf)
            raise RuntimeError("unexpected path")

        service = CertificateLifecycleService(
            db_factory=self.db,
            cluster_provider=lambda cluster_id: self.cluster if cluster_id == 7 else None,
            node_provider=lambda node_id: {"id": node_id, "address": f"192.0.2.{node_id}"},
            remote_file_reader=read_certificate,
        )

        refreshed = asyncio.run(service.refresh(7, username="operator"))

        ca_reads = [node_id for node_id, path in reads if path.endswith("/ca/ca.crt")]
        self.assertTrue(ca_reads)
        self.assertEqual(set(ca_reads), {3})
        self.assertEqual(refreshed["summary"]["failed"], 0)

    def test_refresh_without_a_bootstrap_master_records_generic_failures_without_calling_remote_hosts(self):
        self.cluster["assignments"] = []
        reads: list[tuple[object, object]] = []

        async def read_certificate(node, path):
            reads.append((node, path))
            raise AssertionError("Remote collection must not run without a bootstrap master")

        service = CertificateLifecycleService(
            db_factory=self.db,
            cluster_provider=lambda cluster_id: self.cluster if cluster_id == 7 else None,
            node_provider=lambda node_id: {"id": node_id},
            remote_file_reader=read_certificate,
            completed_run=lambda *_args: 18,
        )

        refreshed = asyncio.run(service.refresh(7, username="operator"))

        self.assertEqual(refreshed["run_id"], 18)
        self.assertEqual(reads, [])
        self.assertGreater(refreshed["summary"]["failed"], 0)
        self.assertEqual(refreshed["summary"]["collected"], 0)

    def test_refresh_redacts_unexpected_remote_reader_failures(self):
        leaked = "-----BEGIN CERTIFICATE----- remote failure material"

        async def read_certificate(_node, _path):
            raise TypeError(leaked)

        service = CertificateLifecycleService(
            db_factory=self.db,
            cluster_provider=lambda cluster_id: self.cluster if cluster_id == 7 else None,
            node_provider=lambda node_id: {"id": node_id},
            remote_file_reader=read_certificate,
        )

        refreshed = asyncio.run(service.refresh(7, username="operator"))

        self.assertGreater(refreshed["summary"]["failed"], 0)
        with self.db() as connection:
            rows = connection.execute(
                "SELECT metadata_json,validation_json,endpoint_json,error_code,error_message "
                "FROM certificate_observations"
            ).fetchall()
        self.assertTrue(rows)
        self.assertNotIn(leaked, " ".join(str(value) for row in rows for value in row))
        self.assertTrue(all(row["error_code"] == "certificate_collection_failed" for row in rows))


if __name__ == "__main__":
    unittest.main()
