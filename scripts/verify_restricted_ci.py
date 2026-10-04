"""Run the disposable-resource restricted CI release gate with telemetry disabled."""

import os
import subprocess
import sys
from pathlib import Path

TESTS = (
    "api/test_ci_principal.py",
    "api/test_ci_principal_migration.py",
    "api/test_ci_rest_boundary.py",
    "api/test_ci_execution.py",
    "api/test_ci_execution_http.py",
    "api/test_ci_execution_migration.py",
    "api/test_ci_subscription.py",
    "api/test_ci_subscription_migration.py",
    "api/test_ci_callback_human_controls.py",
    "api/test_ci_admin_setup.py",
    "models/test_ci_administrator_permissions.py",
    "services/test_ci_execution_admission.py",
    "services/test_ci_execution_dispatch.py",
    "services/test_ci_callback_contract.py",
    "services/test_ci_callback_failures.py",
    "test_system_roles.py",
    "test_restricted_ci_review_example.py",
    "test_ci_setup_review_regressions.py",
)


def main() -> None:
    """Use only the operator's isolated migrated test database."""
    if not os.environ.get("DATABASE_URL"):
        raise SystemExit(
            "Set DATABASE_URL to a migrated disposable PostgreSQL database"
        )
    root = Path(__file__).resolve().parents[1]
    environment = {
        **os.environ,
        "PRELOOP_DISABLE_TELEMETRY": "true",
        "DISABLE_PROPRIETARY_PLUGINS": "true",
        "PYTHONPATH": str(root / "backend"),
        "TZ": "UTC",
    }
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            *("backend/tests/" + name for name in TESTS),
        ],
        cwd=root,
        env=environment,
        check=False,
    )
    raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main()
