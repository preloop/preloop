"""Guards for the development docker-compose healthchecks and start.sh wait."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
START_SH = REPO_ROOT / "start.sh"


def _load_compose() -> Dict[str, Any]:
    return yaml.safe_load(COMPOSE_FILE.read_text())


def test_postgres_and_nats_are_healthchecked() -> None:
    services = _load_compose()["services"]
    postgres = services["postgres"]["healthcheck"]["test"]
    nats = services["nats"]
    assert "pg_isready -U postgres" in " ".join(postgres)
    assert nats["command"] == ["-js", "-m", "8222"]
    assert "healthz" in " ".join(nats["healthcheck"]["test"])


def test_app_services_wait_until_dependencies_are_healthy() -> None:
    services = _load_compose()["services"]
    for name in ("api", "gateway", "scheduler", "worker"):
        depends = services[name]["depends_on"]
        assert depends["postgres"]["condition"] == "service_healthy"
        assert depends["nats"]["condition"] == "service_healthy"


def test_start_sh_waits_for_database_before_init() -> None:
    text = START_SH.read_text()
    wait_at = text.index("wait_for_database")
    init_at = text.index("python scripts/init_db.py")
    assert wait_at < init_at
    assert "DATABASE_URL" in text
