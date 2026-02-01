#!/usr/bin/env python3
"""
Auto-Join Script for Teams Interview Bot

Automates joining a Teams meeting and initializing the interview session.
Calls the bot's join endpoint and the Python sink's session start endpoint.

Last Grunted: 01/31/2026

Usage (run from the python/ directory):
    cd python
    
    # Dry run (test without making requests)
    uv run python ../scripts/auto_join.py \
        --meeting-url "https://teams.microsoft.com/l/meetup-join/..." \
        --candidate-name "John Doe" \
        --dry-run

    # Production run
    uv run python ../scripts/auto_join.py \
        --meeting-url "https://teams.microsoft.com/l/meetup-join/..." \
        --candidate-name "Jane Smith" \
        --bot-endpoint "https://teamsbot.qmachina.com" \
        --sink-endpoint "https://agent.qmachina.com" \
        --display-name "Interview Bot"

Note: This script requires httpx which is installed in the python/ venv.
"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from typing import Optional

import httpx

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Default endpoints
DEFAULT_BOT_ENDPOINT = "https://teamsbot.qmachina.com"
DEFAULT_SINK_ENDPOINT = "https://agent.qmachina.com"  # External FQDN for Python agent
DEFAULT_DISPLAY_NAME = "Interview Bot"

# HTTP timeout settings
HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


async def join_meeting(
    bot_endpoint: str,
    meeting_url: str,
    display_name: str,
    dry_run: bool = False,
) -> bool:
    """
    Call the bot's join endpoint to join the Teams meeting.

    Args:
        bot_endpoint: Base URL of the bot API
        meeting_url: Teams meeting join URL
        display_name: Name to display in the meeting
        dry_run: If True, only print what would be done

    Returns:
        True if successful, False otherwise
    """
    join_url = f"{bot_endpoint.rstrip('/')}/api/calling/join"
    payload = {
        "joinUrl": meeting_url,
        "displayName": display_name,
    }

    if dry_run:
        logger.info(f"[DRY RUN] Would POST to: {join_url}")
        logger.info(f"[DRY RUN] Payload: {payload}")
        return True

    logger.info(f"Joining meeting via: {join_url}")
    logger.info(f"Display name: {display_name}")

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.post(join_url, json=payload)
            response.raise_for_status()

            result = response.json() if response.content else {}
            logger.info(f"Bot join response: {result}")
            return True

    except httpx.ConnectError as e:
        logger.error(f"Failed to connect to bot endpoint: {e}")
        logger.error("Ensure the bot is running and accessible")
        return False
    except httpx.HTTPStatusError as e:
        logger.error(f"Bot returned error status: {e.response.status_code}")
        try:
            error_body = e.response.json()
            logger.error(f"Error details: {error_body}")
        except Exception:
            logger.error(f"Response body: {e.response.text}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error joining meeting: {e}")
        return False


async def start_session(
    sink_endpoint: str,
    candidate_name: str,
    meeting_url: str,
    dry_run: bool = False,
) -> bool:
    """
    Call the sink's session start endpoint to initialize the interview session.

    Args:
        sink_endpoint: Base URL of the Python sink API
        candidate_name: Name of the candidate being interviewed
        meeting_url: Teams meeting URL for reference
        dry_run: If True, only print what would be done

    Returns:
        True if successful, False otherwise
    """
    session_url = f"{sink_endpoint.rstrip('/')}/session/start"
    payload = {
        "candidate_name": candidate_name,
        "meeting_url": meeting_url,
    }

    if dry_run:
        logger.info(f"[DRY RUN] Would POST to: {session_url}")
        logger.info(f"[DRY RUN] Payload: {payload}")
        return True

    logger.info(f"Starting session via: {session_url}")
    logger.info(f"Candidate: {candidate_name}")

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.post(session_url, json=payload)
            response.raise_for_status()

            result = response.json() if response.content else {}
            logger.info(f"Session start response: {result}")
            return True

    except httpx.ConnectError as e:
        logger.error(f"Failed to connect to sink endpoint: {e}")
        logger.error("Ensure the transcript sink is running (uv run python/transcript_sink.py)")
        return False
    except httpx.HTTPStatusError as e:
        logger.error(f"Sink returned error status: {e.response.status_code}")
        try:
            error_body = e.response.json()
            logger.error(f"Error details: {error_body}")
        except Exception:
            logger.error(f"Response body: {e.response.text}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error starting session: {e}")
        return False


async def auto_join(
    meeting_url: str,
    candidate_name: str,
    bot_endpoint: str = DEFAULT_BOT_ENDPOINT,
    sink_endpoint: str = DEFAULT_SINK_ENDPOINT,
    display_name: str = DEFAULT_DISPLAY_NAME,
    dry_run: bool = False,
) -> bool:
    """
    Execute the full auto-join sequence.

    1. Call bot endpoint to join the Teams meeting
    2. Call sink endpoint to start the interview session

    Args:
        meeting_url: Teams meeting join URL
        candidate_name: Name of the candidate
        bot_endpoint: Base URL of the bot API
        sink_endpoint: Base URL of the Python sink API
        display_name: Name to display in the meeting
        dry_run: If True, only print what would be done

    Returns:
        True if all steps successful, False otherwise
    """
    logger.info("=" * 60)
    logger.info("Teams Interview Bot - Auto Join")
    logger.info("=" * 60)
    logger.info(f"Timestamp: {datetime.utcnow().isoformat()}Z")
    logger.info(f"Meeting URL: {meeting_url[:80]}..." if len(meeting_url) > 80 else f"Meeting URL: {meeting_url}")
    logger.info(f"Candidate: {candidate_name}")
    logger.info(f"Bot Endpoint: {bot_endpoint}")
    logger.info(f"Sink Endpoint: {sink_endpoint}")
    logger.info(f"Display Name: {display_name}")
    if dry_run:
        logger.info("MODE: DRY RUN (no actual requests will be made)")
    logger.info("-" * 60)

    # Step 1: Join meeting
    logger.info("Step 1: Joining Teams meeting...")
    join_success = await join_meeting(bot_endpoint, meeting_url, display_name, dry_run)
    if not join_success:
        logger.error("Failed to join meeting. Aborting.")
        return False
    logger.info("Step 1: Complete")

    # Step 2: Start session
    logger.info("Step 2: Starting interview session...")
    session_success = await start_session(sink_endpoint, candidate_name, meeting_url, dry_run)
    if not session_success:
        logger.warning("Failed to start session (sink may not be running)")
        # Don't fail completely - bot may still have joined
        logger.warning("Bot may have joined meeting. Session tracking not active.")

    logger.info("-" * 60)
    if dry_run:
        logger.info("DRY RUN COMPLETE - No actual requests were made")
    else:
        logger.info("Auto-join sequence complete")
    logger.info("=" * 60)

    return join_success


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Auto-join a Teams meeting and start an interview session",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry run (test without making requests)
  uv run scripts/auto_join.py \\
    --meeting-url "https://teams.microsoft.com/l/meetup-join/..." \\
    --candidate-name "John Doe" \\
    --dry-run

  # Production run
  uv run scripts/auto_join.py \\
    --meeting-url "https://teams.microsoft.com/l/meetup-join/..." \\
    --candidate-name "Jane Smith"

  # Custom endpoints
  uv run scripts/auto_join.py \\
    --meeting-url "https://teams.microsoft.com/l/meetup-join/..." \\
    --candidate-name "Bob Builder" \\
    --bot-endpoint "https://my-bot.example.com" \\
    --sink-endpoint "http://localhost:8000"
        """,
    )

    parser.add_argument(
        "--meeting-url",
        required=True,
        help="Teams meeting join URL (required)",
    )

    parser.add_argument(
        "--candidate-name",
        required=True,
        help="Name of the candidate being interviewed (required)",
    )

    parser.add_argument(
        "--bot-endpoint",
        default=DEFAULT_BOT_ENDPOINT,
        help=f"Bot API endpoint (default: {DEFAULT_BOT_ENDPOINT})",
    )

    parser.add_argument(
        "--sink-endpoint",
        default=DEFAULT_SINK_ENDPOINT,
        help=f"Python sink endpoint (default: {DEFAULT_SINK_ENDPOINT})",
    )

    parser.add_argument(
        "--display-name",
        default=DEFAULT_DISPLAY_NAME,
        help=f"Bot display name in meeting (default: {DEFAULT_DISPLAY_NAME})",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without making actual requests",
    )

    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose (debug) logging",
    )

    return parser.parse_args()


def main() -> int:
    """Main entry point."""
    args = parse_args()

    # Set logging level
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Run the async auto-join sequence
    success = asyncio.run(
        auto_join(
            meeting_url=args.meeting_url,
            candidate_name=args.candidate_name,
            bot_endpoint=args.bot_endpoint,
            sink_endpoint=args.sink_endpoint,
            display_name=args.display_name,
            dry_run=args.dry_run,
        )
    )

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
