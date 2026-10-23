"""Explicit Supabase destination gate for the existing Railway PAPER runtime."""

from __future__ import annotations

import base64
import os
import re
import ssl
import tempfile
from pathlib import Path
from typing import Mapping
from urllib.parse import parse_qsl, unquote, urlsplit

from app.core.config import Settings

PROJECT_REF = "phkligpkcitbbefrrotk"
DATABASE_ROLE = "capital_cipher_staging"
CA_PATH = Path("/run/secrets/supabase-ca.crt")


def prepare_ca(environment: Mapping[str, str]) -> None:
    """Install only a supplied, parseable public CA; never fetch trust material."""
    value = environment.get("SUPABASE_CA_CERT_B64", "")
    if not value:
        return
    try:
        certificate = base64.b64decode(value, validate=True).decode("ascii")
        ssl.create_default_context(cadata=certificate)
        if CA_PATH.is_symlink():
            raise ValueError("symlink")
        CA_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=CA_PATH.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(certificate.encode("ascii"))
        temporary.chmod(0o600)
        os.replace(temporary, CA_PATH)
    except Exception:
        raise RuntimeError("SUPABASE_CA_INVALID") from None


def validate_supabase_database(settings: Settings, environment: Mapping[str, str]) -> None:
    """Keep project, role, session endpoint, TLS and connection budget pinned."""
    try:
        parsed = urlsplit(settings.database_url)
        expected = environment.get("STAGING_EXPECTED_DATABASE_HOST", "").strip().lower()
        direct = f"db.{PROJECT_REF}.supabase.co"
        host = (parsed.hostname or "").lower()
        pooler = re.fullmatch(r"aws-\d+-sa-east-1\.pooler\.supabase\.com", host)
        role = unquote(parsed.username or "")
        options = parse_qsl(parsed.query, keep_blank_values=True)
        tls = dict(options)
        if (
            parsed.scheme != "postgresql+asyncpg"
            or not expected or host != expected
            or not (host == direct or pooler)
            or (parsed.port or 5432) != 5432
            or parsed.path != "/postgres" or parsed.fragment
            or role != (DATABASE_ROLE if host == direct else f"{DATABASE_ROLE}.{PROJECT_REF}")
            or len(options) != 2 or len(tls) != 2
            or tls != {"sslmode": "verify-full", "sslrootcert": str(CA_PATH)}
        ):
            raise ValueError("destination")
        password = unquote(parsed.password or "")
        if (len(password) < 32 or len(set(password)) < 8
            or any(word in password.lower() for word in ("change", "replace", "example", "placeholder"))):
            raise ValueError("credential")
        if settings.database_pool_size > 4 or settings.database_max_overflow != 0:
            raise ValueError("connection budget")
        if not CA_PATH.is_file() or CA_PATH.is_symlink():
            raise ValueError("certificate")
        context = ssl.create_default_context(cafile=str(CA_PATH))
        if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
            raise ValueError("TLS verification")
    except Exception:
        raise RuntimeError("RAILWAY_SUPABASE_DATABASE_BOUNDARY_INVALID") from None
