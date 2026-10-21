#!/usr/bin/env python
"""
Script to initialize the Preloop database.
This creates all tables using Alembic migrations and sets up default models.
"""

import sys
import os
import subprocess
import logging
import yaml

import click
from dotenv import load_dotenv
from sqlalchemy import text

from preloop.models.db.session import get_engine, get_db_session
from preloop.models.crud import crud_embedding_model
from preloop.models.crud import crud_ai_model
from preloop.models import models
from sqlalchemy.orm import Session

from preloop.models.db.vector_types import TRUNCATED_VECTOR_SIZE

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def _default_gateway_alias(
    provider_name: str | None, model_identifier: str | None
) -> str:
    """Build the default gateway alias for one AI model."""
    provider = (provider_name or "openai").strip().lower()
    identifier = (model_identifier or "").strip()
    return f"{provider}/{identifier}" if identifier else provider


def _merge_gateway_meta(
    meta_data: dict | None,
    *,
    provider_name: str | None,
    model_identifier: str | None,
    provider_adapter: str = "preloop",
) -> dict:
    """Add missing gateway defaults without replacing operator configuration."""
    merged = dict(meta_data or {})
    gateway_meta = (
        dict(merged.get("gateway")) if isinstance(merged.get("gateway"), dict) else {}
    )
    gateway_meta.setdefault("enabled", True)
    gateway_meta["provider_adapter"] = (
        gateway_meta.get("provider_adapter") or provider_adapter
    )
    gateway_meta["model_alias"] = gateway_meta.get(
        "model_alias"
    ) or _default_gateway_alias(provider_name, model_identifier)
    merged["gateway"] = gateway_meta
    return merged


def reconcile_ai_model_gateway_settings(
    db_session: Session,
    *,
    provider_adapter: str = "preloop",
) -> int:
    """Initialize routing defaults while preserving disabled or retired models."""
    updated_count = 0
    for ai_model in crud_ai_model.get_gateway_bootstrap_candidates(db_session):
        if not (ai_model.api_key or ai_model.credentials_secret_id):
            continue
        metadata = ai_model.meta_data or {}
        gateway = metadata.get("gateway")
        if metadata.get("retired") is True or (
            isinstance(gateway, dict) and gateway.get("enabled") is False
        ):
            continue
        updated_meta = _merge_gateway_meta(
            ai_model.meta_data,
            provider_name=ai_model.provider_name,
            model_identifier=ai_model.model_identifier,
            provider_adapter=provider_adapter,
        )
        if updated_meta != (ai_model.meta_data or {}):
            crud_ai_model.update(
                db_session, db_obj=ai_model, obj_in={"meta_data": updated_meta}
            )
            updated_count += 1
    return updated_count


def seed_plan_catalog(db_session, project_root: str) -> None:
    """Seed current catalog terms without rewriting grandfathered contracts."""
    plans_file = os.path.join(project_root, "plans.yaml")
    if not os.path.exists(plans_file):
        click.echo("plans.yaml not found, skipping plan catalog initialization.")
        return

    with open(plans_file, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    plans = raw.get("plans") or []

    for plan_data in plans:
        if not isinstance(plan_data, dict) or not plan_data.get("id"):
            continue
        plan_id = str(plan_data["id"])
        plan = db_session.query(models.Plan).filter(models.Plan.id == plan_id).first()
        legacy = plan_data.get("legacy") is True
        if plan is not None and legacy:
            # Upgrade hooks run this seed repeatedly. The catalog baseline must
            # not replace negotiated prices, features or retention promises.
            plan.is_active = False
            continue
        is_active = not legacy and bool(plan_data.get("is_active", True))
        stripe_product_id = plan_data.get("stripe_product_id")
        if stripe_product_id is None and (
            plan_data.get("price_monthly") is not None
            or plan_data.get("price_annually") is not None
        ):
            stripe_product_id = plan_id

        if plan is None:
            plan = models.Plan(
                id=plan_id,
                name=plan_data["name"],
                price_monthly=plan_data.get("price_monthly"),
                price_annually=plan_data.get("price_annually"),
                is_active=is_active,
                is_custom=False,
                features=plan_data.get("features") or {},
                stripe_product_id=stripe_product_id,
            )
            db_session.add(plan)
        else:
            plan.is_active = is_active
            plan.name = plan_data["name"]
            plan.price_monthly = plan_data.get("price_monthly")
            plan.price_annually = plan_data.get("price_annually")
            plan.features = plan_data.get("features") or {}
            if stripe_product_id is not None:
                plan.stripe_product_id = stripe_product_id

    db_session.commit()


@click.command()
@click.option("--force", is_flag=True, help="Skip confirmation")
def init_db(force: bool):
    """
    Initialize the database by creating all tables using Alembic
    and setting up default embedding and AI models.
    """
    # Load environment variables
    load_dotenv()

    # Get database connection string from environment
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        click.echo("ERROR: DATABASE_URL environment variable not set.")
        sys.exit(1)

    if not force:
        click.echo("This will create all database tables using Alembic migrations.")
        click.echo(f"Database: {db_url}")
        if not click.confirm("Continue?"):
            click.echo("Operation cancelled.")
            sys.exit(0)

    try:
        # Get the project root (parent of scripts directory)
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # Alembic config is in backend/preloop/models/
        alembic_dir = os.path.join(project_root, "backend", "preloop", "models")

        # Run alembic upgrade head
        click.echo("Running Alembic migrations to create database tables...")
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=alembic_dir,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            click.echo(f"ERROR: Alembic migration failed: {result.stderr}")
            sys.exit(1)

        click.echo("Database tables created successfully via Alembic!")

        # Get engine for post-migration setup
        engine = get_engine()

        # Add the adaptive similarity search index to the database
        click.echo("Creating vector similarity search index...")
        with engine.connect() as connection:
            connection.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS idx_issueembedding_vector_{vector_size} ON issueembedding
                    using hnsw ((subvector(embedding, 1, {vector_size})::vector({vector_size})) vector_cosine_ops)
                    with (m = 16, ef_construction = 40);
                    """.format(vector_size=TRUNCATED_VECTOR_SIZE)
                )
            )
            connection.commit()

        click.echo("Vector search index created successfully!")
        db_session = next(get_db_session())

        click.echo("Initializing plan catalog from plans.yaml...")
        seed_plan_catalog(db_session, project_root)
        click.echo("Plan catalog initialized successfully!")

        # Initialize system roles and permissions
        click.echo("Initializing system roles and permissions...")
        # Import here to avoid circular imports
        # init_system_roles.py is in the same directory as this script
        local_scripts_dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, local_scripts_dir)
        from init_system_roles import initialize_system_roles

        if initialize_system_roles(db_session):
            click.echo("System roles and permissions initialized successfully!")
        else:
            click.echo("WARNING: Failed to initialize system roles and permissions")

        # Embedding model setup
        provider = os.getenv("EMBEDDING_PROVIDER", "openai")
        model_name = os.getenv("EMBEDDING_MODEL_NAME", "text-embedding-3-small")
        api_key = os.getenv("OPENAI_API_KEY")
        dimensions = int(os.getenv("EMBEDDING_DIMENSIONS", "1536"))
        version = os.getenv("EMBEDDING_VERSION", model_name)

        if api_key:
            existing_model = crud_embedding_model.get_by_provider_version(
                db_session, provider=provider, version=version
            )
            if existing_model:
                click.echo(
                    f"Embedding model '{model_name}' already exists (ID: {existing_model.id}), skipping."
                )
            else:
                click.echo(f"Adding embedding model '{model_name}'...")
                model_data = {
                    "name": model_name,
                    "provider": provider,
                    "dimensions": dimensions,
                    "version": version,
                    "is_active": True,
                    "meta_data": {"api_key": api_key, "model": version},
                }
                crud_embedding_model.create(db_session, obj_in=model_data)
                click.echo(f"Embedding model '{model_name}' created successfully.")
        else:
            click.echo("OPENAI_API_KEY not set, skipping embedding model creation.")

        # AI model setup
        ai_provider = os.getenv("AI_PROVIDER", "openai")
        ai_model_name = os.getenv("AI_MODEL_NAME", "gpt-5.3-codex")
        ai_model_api_key = os.getenv("OPENAI_API_KEY")
        ai_model_api_url = os.getenv("AI_API_URL", "https://api.openai.com/v1")
        ai_model_version = os.getenv("AI_MODEL_VERSION", ai_model_name)

        if ai_model_api_key:
            if not crud_ai_model.default_model_exists(db_session):
                click.echo(f"Adding default AI model '{ai_model_name}'...")
                ai_model_data = {
                    "name": ai_model_name,
                    "provider_name": ai_provider,
                    "api_key": ai_model_api_key,
                    "api_endpoint": ai_model_api_url,
                    "model_identifier": ai_model_version,
                    "is_default": True,
                }
                ai_model_data["meta_data"] = _merge_gateway_meta(
                    None,
                    provider_name=ai_provider,
                    model_identifier=ai_model_version,
                )
                crud_ai_model.create_with_account(
                    db=db_session,
                    obj_in=ai_model_data,
                    account_id=None,
                )
                click.echo(f"Default AI model '{ai_model_name}' created successfully.")
            else:
                click.echo("Default AI model already exists, skipping.")
        else:
            click.echo("OPENAI_API_KEY not set, skipping AI model creation.")

        updated_models = reconcile_ai_model_gateway_settings(db_session)
        click.echo(f"Ensured gateway routing metadata on {updated_models} AI model(s).")

    except Exception as e:
        click.echo(f"ERROR: Failed to initialize database: {str(e)}")
        import traceback

        traceback.print_exc()
        sys.exit(1)

    click.echo("\nDatabase initialization complete!")


if __name__ == "__main__":
    init_db()
