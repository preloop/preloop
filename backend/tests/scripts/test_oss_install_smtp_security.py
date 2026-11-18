"""The installer keeps ``SMTP_SECURITY`` across re-runs.

A re-run that rewrites the ``SMTP_*`` block must not drop a chosen transport
mode and silently fall back to STARTTLS. All values are synthetic.
"""

from pathlib import Path

from tests.scripts.test_oss_install_env_escape import _run_installer_sh


def test_load_existing_env_reads_smtp_security(tmp_path: Path) -> None:
    """protects=re-run upgrade keeps ssl; fails_when=the value is not loaded."""
    (tmp_path / ".env").write_text(
        "SMTP_HOST=mail.example.com\nSMTP_SECURITY=ssl\n", encoding="utf-8"
    )
    result = _run_installer_sh(
        'load_existing_env; printf "%s" "$SMTP_SECURITY"\n',
        install_dir=tmp_path,
        extra_env={"SMTP_SECURITY": ""},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ssl"


def test_smtp_rewrite_writes_smtp_security() -> None:
    """protects=both .env writers include the mode; fails_when=it is omitted."""
    text = Path("scripts/install-oss.sh").read_text(encoding="utf-8")
    assert text.count('compose_env_assign SMTP_SECURITY "$SMTP_SECURITY"') == 2
