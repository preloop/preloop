"""
Example MCP Server for testing external MCP server integration.

This is a simple MCP server built with FastMCP that provides a few example tools
for testing the Phase 1B external MCP server functionality in Preloop.

The server supports HTTP streaming transport with bearer token authentication.

To run this server:
    python examples/example_mcp_server.py

The server will start on http://localhost:8001
"""

import hashlib
import logging
import os
import random
from datetime import date, timedelta
from typing import List, Optional

from fastmcp import FastMCP

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Get bearer token from environment
BEARER_TOKEN = os.getenv("BEARER_TOKEN", "test-token-12345")

# Create FastMCP server instance
# Note: Authentication/middleware is not supported in current FastMCP API
# The server will be publicly accessible - secure with network policies if needed
mcp = FastMCP("Example MCP Server")


@mcp.tool()
def pay(recipient: str, amount: int) -> str:
    """Pay the recipient the specified amount in USD.

    Args:
        recipient: The recipient of the payment
        amount: The amount to pay

    Returns:
        A message indicating the success of the payment
    """
    return f"Payment of ${amount} to {recipient} completed successfully"


@mcp.tool()
def rollback_deployment(env: str = "production") -> str:
    """Rollback the deployment to the previous version."""
    return f"Deployment of {env} environment rolled back successfully"


@mcp.tool()
def send_email(recipient: str, subject: str, body: str) -> str:
    """Send an email to the specified recipient."""
    return f"Email sent to {recipient} with subject {subject}"


@mcp.tool()
def refund_order(order_id: str) -> str:
    """Refund the specified order."""
    return f"Order {order_id} refunded successfully"


@mcp.tool()
def verify_refund_eligibility(order_id: str) -> str:
    """Verify if the specified order is eligible for refund."""
    return f"Order {order_id} is eligible for refund"


# -----------------------------------------------------------------------------
# Research fixture: a dummy "events database" lookup tool.
#
# Simulates an Elasticsearch-backed corpus of records about a person. Returns
# deterministic synthetic data (seeded by person+topic) so demos are
# reproducible with no external API key or dependency. Each record carries a
# verbose `details` blob so the tool output is genuinely token-heavy — this is
# what lets a research agent's context balloon, which the Preloop cost
# optimizer then surfaces.
#
# NOTE: All returned data is synthetic/fictional and intended only for testing.
# -----------------------------------------------------------------------------
_TOPIC_TEMPLATES = {
    "investments": [
        (
            "investment",
            "Acquired stake in {org}",
            "Took a {pct}% position in {org}, valued around ${amt}M.",
        ),
        (
            "funding",
            "Backed {org}'s Series {ser}",
            "Participated in {org}'s ${amt}M Series {ser} round.",
        ),
        (
            "divestment",
            "Exited {org}",
            "Sold remaining holdings in {org} after a {pct}% gain.",
        ),
    ],
    "statements": [
        (
            "statement",
            "Comments on {topic_word}",
            "Publicly addressed {topic_word} at the {event} event.",
        ),
        (
            "interview",
            "Interview with {outlet}",
            "Discussed {topic_word} and future plans in a {outlet} interview.",
        ),
        (
            "op-ed",
            "Op-ed on {topic_word}",
            "Authored an opinion piece arguing for reform in {topic_word}.",
        ),
    ],
    "business": [
        (
            "deal",
            "Closed deal with {org}",
            "Signed a multi-year partnership with {org}.",
        ),
        (
            "appointment",
            "Joined {org} board",
            "Appointed to the board of directors at {org}.",
        ),
        (
            "launch",
            "Launched {org}",
            "Founded {org}, a venture focused on {topic_word}.",
        ),
    ],
    "tech": [
        (
            "product",
            "Unveiled {org} platform",
            "Announced a new platform under {org} targeting {topic_word}.",
        ),
        (
            "patent",
            "Filed patent",
            "Listed as inventor on a patent related to {topic_word}.",
        ),
        (
            "acquisition",
            "Acquired {org}",
            "Bought {org} to expand {topic_word} capabilities.",
        ),
    ],
    "sports": [
        (
            "transfer",
            "Linked to {org}",
            "Reported in talks with {org} over a ${amt}M move.",
        ),
        (
            "performance",
            "Standout at {event}",
            "Delivered a notable performance at {event}.",
        ),
        (
            "sponsorship",
            "Signed with {brand}",
            "Agreed a multi-year endorsement deal with {brand}.",
        ),
    ],
    "general": [
        (
            "award",
            "Received recognition",
            "Honoured at {event} for contributions to {topic_word}.",
        ),
        ("appearance", "Appeared at {event}", "Featured as a speaker at {event}."),
        (
            "controversy",
            "Faced scrutiny",
            "Subject of reporting regarding {topic_word}.",
        ),
    ],
}
_ORGS = [
    "NovaChip",
    "Helio Capital",
    "Vertex Labs",
    "BlueRiver",
    "Meridian Group",
    "Atlas Ventures",
    "Quanta Dynamics",
    "Orbit Foods",
    "Summit Health",
    "Pyra AI",
]
_OUTLETS = [
    "The Ledger",
    "TechWire",
    "Global Times Daily",
    "Frontier Review",
    "MarketPulse",
]
_EVENTS = [
    "the Davos summit",
    "TechCon",
    "the Q3 investor day",
    "a charity gala",
    "an industry panel",
]
_BRANDS = ["Apex", "Stride", "Volt", "Nimbus", "Crestline"]


def _pick_templates(topic: Optional[str]):
    if topic:
        key = topic.lower()
        for cat, templates in _TOPIC_TEMPLATES.items():
            if cat in key or key in cat:
                return templates, topic
    return _TOPIC_TEMPLATES["general"], (topic or "public life")


def _synthetic_events(person: str, topic: Optional[str], n: int) -> List[dict]:
    seed = int(hashlib.sha256(f"{person}|{topic}".encode()).hexdigest(), 16) % (2**32)
    rng = random.Random(seed)
    templates, topic_word = _pick_templates(topic)
    events: List[dict] = []
    for i in range(n):
        etype, title_t, summ_t = rng.choice(templates)
        fill = {
            "org": rng.choice(_ORGS),
            "outlet": rng.choice(_OUTLETS),
            "event": rng.choice(_EVENTS),
            "brand": rng.choice(_BRANDS),
            "topic_word": topic_word,
            "pct": rng.randint(3, 40),
            "amt": rng.choice([5, 12, 25, 60, 120, 340]),
            "ser": rng.choice(["A", "B", "C"]),
        }
        evt_date = date.today() - timedelta(days=rng.randint(30, 1800))
        title = title_t.format(**fill)
        summary = summ_t.format(**fill)
        # Verbose raw-record blob so each result is token-heavy, mimicking the
        # full _source document an Elasticsearch query would hand back.
        details = (
            f"FULL RECORD | subject={person} | type={etype} | "
            f"headline={title} | body={summary} "
            f"Cross-referenced against {rng.randint(3, 18)} adjacent records in the "
            f"index. Corroborating sources: {rng.choice(_OUTLETS)}, "
            f"{rng.choice(_OUTLETS)}. Geographic context spans "
            f"{rng.choice(['EMEA', 'AMER', 'APAC'])}. "
            f"Sentiment analysis: {rng.choice(['positive', 'neutral', 'mixed'])}. "
            f"Entity links: {', '.join(rng.sample(_ORGS, 3))}. "
            f"Retrieval score {round(rng.uniform(0.4, 0.99), 3)}; the record was "
            f"surfaced from shard {rng.randint(0, 7)} and includes "
            f"{rng.randint(120, 900)} tokens of raw narrative context that the "
            f"agent must carry forward in its working memory for the rest of the run."
        )
        # Obviously-unused, bulky fields that a research report never reads.
        # These exist so the optimizer's unused-output detection has something
        # to flag and the tool output filter has something to strip. All
        # deterministic via the seeded ``rng``.
        raw_record_html = (
            "<div class='es-record'><table>"
            + "".join(
                f"<tr><td class='k'>field_{rng.randint(0, 999)}</td>"
                f"<td class='v'>{rng.choice(_OUTLETS)}-"
                f"{rng.randint(10000, 99999)}</td></tr>"
                for _ in range(12)
            )
            + "</table><pre>"
            + " ".join(rng.choice(_ORGS).replace(" ", "_") for _ in range(40))
            + "</pre></div>"
        )
        embedding = [round(rng.uniform(-1.0, 1.0), 6) for _ in range(64)]
        internal_scores = {
            "shard_affinity": round(rng.uniform(0.0, 1.0), 4),
            "rerank_delta": round(rng.uniform(-0.5, 0.5), 4),
            "index_generation": rng.randint(1, 50),
            "retrieval_cost_ms": rng.randint(2, 180),
        }
        events.append(
            {
                "date": evt_date.isoformat(),
                "type": etype,
                "title": title,
                "summary": summary,
                "source": f"es://events/{seed % 100000}/{i}",
                "confidence": round(rng.uniform(0.55, 0.97), 2),
                "details": details,
                "raw_record_html": raw_record_html,
                "embedding": embedding,
                "internal_scores": internal_scores,
            }
        )
    events.sort(key=lambda e: e["date"], reverse=True)
    return events


@mcp.tool()
def get_person_events(
    person: str,
    topic: Optional[str] = None,
    identifier: Optional[str] = None,
    max_events: int = 6,
) -> List[dict]:
    """Return events from the events database related to a person.

    Args:
        person: Full name of the person to look up.
        topic: Optional thematic filter (e.g. 'investments', 'statements', 'tech').
        identifier: Optional disambiguating info (e.g. role, company).
        max_events: Maximum number of events to return.

    Returns:
        A list of event records, each with date, type, title, summary, source,
        confidence, and a verbose details blob.

    NOTE: This is a DUMMY tool. All returned data is synthetic/fictional and
    intended only for testing the research agent.
    """
    n = max(1, min(int(max_events), 12))
    return _synthetic_events(person, topic, n)


if __name__ == "__main__":
    logger.info("=" * 70)
    logger.info("Starting Example MCP Server on http://localhost:8001")
    logger.info("=" * 70)
    logger.info("")
    logger.info("⚠️  WARNING: No authentication configured (FastMCP limitation)")
    logger.info("   Secure with network policies or firewall rules if needed")
    logger.info("")
    logger.info("Available tools:")
    logger.info("  - pay: Pay a recipient (approval-demo tool)")
    logger.info("  - rollback_deployment: Roll back a deployment")
    logger.info("  - send_email: Send an email")
    logger.info("  - refund_order: Refund an order")
    logger.info("  - verify_refund_eligibility: Check refund eligibility")
    logger.info(
        "  - get_person_events: Look up events about a person (research fixture)"
    )
    logger.info("")
    logger.info("To add this server to Preloop:")
    logger.info("  1. Navigate to /console/tools in Preloop UI")
    logger.info("  2. Click 'Add MCP Server'")
    logger.info("  3. Enter:")
    logger.info("     - Name: Example MCP Server")
    logger.info("     - URL: http://host.docker.internal:8001")
    logger.info("            (or http://localhost:8001 if running locally)")
    logger.info("     - Transport: http-streaming")
    logger.info("     - Auth Type: none")
    logger.info("     - Status: active")
    logger.info("  4. Click 'Add' and then 'Scan' to discover tools")
    logger.info("")
    logger.info("=" * 70)

    # Run with FastMCP's built-in server using streamable HTTP transport
    # This creates a FastAPI app internally with the correct MCP protocol handlers
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=8001,
    )
