"""Built-in pre-check types (#2541), one module per check.

Importing this package registers every built-in check with
``preloop.services.flow_precheck.CHECKS``. A later check is one module here
and one line below.
"""

from preloop.services.flow_precheck import register_check
from preloop.services.flow_precheck_checks.default_branch_ci import (
    evaluate_default_branch_ci,
)
from preloop.services.flow_precheck_checks.failed_ci_runs import (
    evaluate_failed_ci_runs,
)
from preloop.services.flow_precheck_checks.issues import evaluate_issues
from preloop.services.flow_precheck_checks.pull_requests import (
    evaluate_open_pull_requests,
)

register_check("open_pull_requests", evaluate_open_pull_requests)
register_check("default_branch_ci", evaluate_default_branch_ci)
register_check("failed_ci_runs", evaluate_failed_ci_runs)
register_check("issues", evaluate_issues)

__all__ = [
    "evaluate_default_branch_ci",
    "evaluate_failed_ci_runs",
    "evaluate_issues",
    "evaluate_open_pull_requests",
]
