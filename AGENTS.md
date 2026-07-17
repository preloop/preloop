# Preloop Development Guide

Only use the DB models defined in the preloop.models package `from preloop.models import models`
Do not access the DB directly in backend code. Always use the CRUD layer at `preloop.models.crud`

Use the Lit.dev framework for frontend code. If you create new web components ensure that the landing page content is not hidden in their shadow DOM.

## Commands
- **Activate venv**: `source .venv/bin/activate || source ../.venv/bin/activate`
- **Install**: `pip install -e ".[dev]"`
- **Run server**: `python -m preloop.server`
- **Run tests**: `pytest`
- **Run single test**: `pytest tests/path/to/test_file.py::TestClass::test_function`
- **Lint**: `ruff check .`
- **Format**: `ruff format .`
- **Type check**: `mypy backend tests`
- **Docker development**: `docker-compose up`
- **Install pre-commit**: `pre-commit install`
- **PostgreSQL access**: `docker compose exec postgres psql -U postgres -d preloop`
- **Database migrations**: `alembic upgrade head` (from backend/preloop/models)

## Git Workflow

- **NEVER use git push in any form unless explicitly requested by the user**
- After making changes, present them to the user for review before any git operations beyond committing locally
- After making significant changes, consider their impact on README.md and ARCHITECTURE.md and update these files accordingly.

## Code Style
- **Formatting**: Ruff format with 88 character line length
- **Imports**: Use isort with black profile, group stdlib/third-party/local
- **Types**: Use strict typing with mypy, all functions must have type annotations
- **Naming**: snake_case for variables/functions, PascalCase for classes, UPPER_CASE for constants
- **Error handling**: Use specific exceptions, log with appropriate level, handle async errors properly
- **Docstrings**: Google-style with type annotations, document params, returns, raises
- **Async**: Use async for I/O-bound operations, run_async utility for sync contexts
- **Testing**: All code changes should have corresponding tests. Use red/green TDD when possible.
- **Test telemetry**: ALL test scripts, rigs, and scripted runs must set `PRELOOP_DISABLE_TELEMETRY=true` (CLI, installers, and instance `.env`) so test traffic never pollutes funnel/adoption telemetry.

## Pre-commit Hooks
The project uses pre-commit hooks to ensure code quality. These hooks run automatically before each commit and include:
- Code formatting with ruff format
- Import sorting with isort
- Linting with ruff
- Various file checks (trailing whitespace, YAML validity, etc.)

To use pre-commit:
1. Install pre-commit: `pip install pre-commit`
2. Install the hooks: `pre-commit install`
3. The hooks will run automatically on git commit
4. To run hooks manually: `pre-commit run --all-files`
5. Activate venv before committing or running pre-commit
