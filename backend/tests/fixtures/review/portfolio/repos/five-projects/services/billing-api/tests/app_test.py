"""synthetic fixture."""

from billing_api.app import main


def test_main_runs(capsys):
    main()
    assert "billing-api" in capsys.readouterr().out
