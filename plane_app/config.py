"""Environment configuration for the app."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    data_dir: Path
    admin_password: str
    secret_key: str
    engine_url: str
    engine_key: str

    @classmethod
    def from_env(cls) -> "Config":
        data_dir = Path(os.environ.get("DATA_DIR", "./data"))
        data_dir.mkdir(parents=True, exist_ok=True)
        secret = os.environ.get("SECRET_KEY")
        if not secret:
            f = data_dir / "secret_key"
            if not f.exists():
                tmp = f.with_suffix(".tmp")
                tmp.write_text(secrets.token_urlsafe(48))
                os.replace(tmp, f)
            secret = f.read_text().strip()
        password = os.environ.get("ADMIN_PASSWORD")
        if not password:
            if os.environ.get("APP_ENV") == "production":
                raise RuntimeError("ADMIN_PASSWORD must be set in production")
            password = "admin"
        return cls(data_dir=data_dir, admin_password=password, secret_key=secret,
                   engine_url=os.environ.get("ENGINE_URL", "http://localhost:8000"),
                   engine_key=os.environ.get("ENGINE_KEY", ""))
