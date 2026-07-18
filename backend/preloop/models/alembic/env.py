import os
from logging.config import fileConfig

from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

from alembic import context

# Import Base from your models
from preloop.models.models.base import Base

# Import all models so they register with Base.metadata for autogenerate.
import importlib

_MODEL_MODULES = (
    "issue",
    "organization",
    "project",
    "tracker",
    "account",
    "agent_control_command",
    "api_key",
    "api_usage",
    "client_version_log",
    "comment",
    "ai_model",
    "issue_duplicate",
    "model_price_override",
    "provider_billing",
)
for _model_module in _MODEL_MODULES:
    importlib.import_module(f"preloop.models.models.{_model_module}")

# Load .env file from the parent directory (project root)
load_dotenv(
    dotenv_path=os.path.join(os.path.dirname(__file__), "..", "..", "..", ".env")
)
# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
target_metadata = Base.metadata  # Set target metadata for autogenerate

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.

# Get database URL from environment variable
database_url = os.getenv("DATABASE_URL")
if not database_url:
    # Fallback or raise error if DATABASE_URL is crucial for migrations
    # For now, let's use the default from config.py as a fallback
    # but ideally, migrations should fail if the URL isn't explicitly set.
    print(
        "Warning: DATABASE_URL not found in environment. "
        "Using default postgresql+psycopg://postgres:postgres@localhost/preloop. "
        "Ensure DATABASE_URL is set in your .env file or environment."
    )
    database_url = "postgresql+psycopg://postgres:postgres@localhost/preloop"

# Set the database URL in the Alembic config object
config.set_main_option("sqlalchemy.url", database_url)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
