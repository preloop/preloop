"""Database setup utilities."""

import logging
import os
import subprocess
from typing import Optional

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from .session import get_engine, redact_secrets_in_text

logger = logging.getLogger(__name__)


def setup_database(database_url: Optional[str] = None) -> None:
    """Set up the database schema using Alembic migrations.

    Creates all tables and initializes the PGVector extension.
    """
    try:
        # Get the database engine
        engine = get_engine(database_url)

        # Only create the pgvector extension if using PostgreSQL
        if database_url and "postgresql" in database_url:
            try:
                with engine.connect() as conn:
                    conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
                    conn.commit()
                logger.info("PGVector extension created successfully")
            except Exception as e:
                logger.warning(
                    "Failed to create vector extension: "
                    f"{redact_secrets_in_text(e, database_url)}"
                )

        # Get the models directory (one level up from db/)
        # alembic.ini is in backend/preloop/models/
        models_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        # Run alembic upgrade head
        logger.info("Running Alembic migrations...")
        result = subprocess.run(
            ["alembic", "upgrade", "head"],
            cwd=models_dir,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            stderr = redact_secrets_in_text(
                result.stderr, database_url or os.getenv("DATABASE_URL")
            )
            logger.error(f"Alembic migration failed: {stderr}")
            raise RuntimeError(f"Alembic migration failed: {stderr}")

        logger.info("Database schema created successfully via Alembic")
    except SQLAlchemyError as e:
        logger.error(
            "Error setting up database: "
            f"{redact_secrets_in_text(e, database_url or os.getenv('DATABASE_URL'))}"
        )
        raise


def reset_database(database_url: Optional[str] = None) -> None:
    """Reset the database schema using Alembic.

    Downgrades to base and then upgrades to head. Use with caution!
    """
    try:
        # Get the models directory (one level up from db/)
        models_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        # Downgrade to base
        logger.info("Downgrading database to base...")
        result = subprocess.run(
            ["alembic", "downgrade", "base"],
            cwd=models_dir,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            stderr = redact_secrets_in_text(
                result.stderr, database_url or os.getenv("DATABASE_URL")
            )
            logger.error(f"Alembic downgrade failed: {stderr}")
            raise RuntimeError(f"Alembic downgrade failed: {stderr}")

        logger.info("Database schema dropped successfully")

        # Recreate tables
        setup_database(database_url)
    except SQLAlchemyError as e:
        logger.error(
            "Error resetting database: "
            f"{redact_secrets_in_text(e, database_url or os.getenv('DATABASE_URL'))}"
        )
        raise


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    setup_database()
    logger.info("Database setup complete")
