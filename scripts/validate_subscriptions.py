"""
Validates the consistency of active subscriptions between the local database and Stripe.

This script fetches all active subscriptions from both sources and reports on any
discrepancies, such as:
- Subscriptions active in Stripe but not in the local database.
- Subscriptions active in the local database but not in Stripe.
"""

import os
import sys
import stripe


from preloop.config import settings
from preloop.models.models import Subscription
from preloop.models.db.session import get_db_session


# Import preloop from THIS repo's backend/, ahead of any installed package
# (an editable install may point at a different checkout).
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(project_root, "backend"))

# --- Configuration ---
STRIPE_API_KEY = settings.stripe_secret_key
DATABASE_URL = settings.database.url


def validate_subscriptions():
    """
    Main function to orchestrate the validation of subscriptions.
    """
    print("Starting subscription validation...")

    # 1. Configure Stripe API
    stripe.api_key = STRIPE_API_KEY
    print("Stripe API key configured.")

    # 2. Connect to the database
    try:
        db = next(get_db_session())
        print("Successfully connected to the database.")
    except Exception as e:
        print(f"ERROR: Could not connect to the database: {e}")
        return

    # 3. Fetch active subscriptions from Stripe
    try:
        stripe_subscriptions = stripe.Subscription.list(status="active", limit=100)
        stripe_sub_ids = {s.id for s in stripe_subscriptions.data}
        print(f"Found {len(stripe_sub_ids)} active subscriptions in Stripe.")
    except Exception as e:
        print(f"ERROR: Could not fetch subscriptions from Stripe: {e}")
        return

    # 4. Fetch active subscriptions from the local database
    local_subscriptions = (
        db.query(Subscription).filter(Subscription.status == "active").all()
    )
    local_sub_ids = {s.stripe_subscription_id for s in local_subscriptions}
    print(f"Found {len(local_sub_ids)} active subscriptions in the local database.")

    # 5. Compare the two sets of subscriptions
    print("\n--- Validation Report ---")

    # Subscriptions in Stripe but not in the local DB
    missing_in_local = stripe_sub_ids - local_sub_ids
    if missing_in_local:
        print(
            f"\nWARNING: {len(missing_in_local)} subscriptions found in Stripe but not in the local database:"
        )
        for sub_id in missing_in_local:
            print(f"  - {sub_id}")
    else:
        print(
            "\nOK: All active Stripe subscriptions are present in the local database."
        )

    # Subscriptions in the local DB but not in Stripe
    missing_in_stripe = local_sub_ids - stripe_sub_ids
    if missing_in_stripe:
        print(
            f"\nERROR: {len(missing_in_stripe)} subscriptions found in the local database but not in Stripe:"
        )
        for sub_id in missing_in_stripe:
            print(f"  - {sub_id}")
    else:
        print("\nOK: All active local subscriptions are present in Stripe.")

    print("\nSubscription validation completed.")


if __name__ == "__main__":
    validate_subscriptions()
