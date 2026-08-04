"""Runtime helpers for CA-verified controller-to-Elastic connections."""

from __future__ import annotations

import ssl
from pathlib import Path


def ca_ssl_context(ca_path: str | Path, *, ssl_module=ssl):
    """Build a CA-verified TLS context for an Elastic endpoint.

    The strict X.509 flag is relaxed only for compatibility with older
    controller-generated CAs; certificate and hostname verification remain
    enabled by ``create_default_context``.
    """

    context = ssl_module.create_default_context(cafile=str(ca_path))
    if hasattr(ssl_module, "VERIFY_X509_STRICT"):
        context.verify_flags &= ~ssl_module.VERIFY_X509_STRICT
    return context


def cluster_ca_path(cache_dir: str | Path, cluster_id: int) -> Path:
    """Return the controller cache path for one cluster's CA certificate."""

    return Path(cache_dir) / f"cluster-{cluster_id}.crt"


def invalidate_cluster_ca(cache_dir: str | Path, cluster_id: int) -> None:
    """Remove a cached cluster CA, if present, without failing cleanup."""

    cluster_ca_path(cache_dir, cluster_id).unlink(missing_ok=True)
