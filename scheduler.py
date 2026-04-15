"""Daily scheduler — runs BaluAgent at 9:00 AM CST every day."""
import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import schedule
from rich.console import Console
from rich.logging import RichHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[RichHandler(rich_tracebacks=True)],
)
logger = logging.getLogger("BaluAgent.Scheduler")
console = Console()

CST = ZoneInfo("America/Chicago")


def _now_cst() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S CST")


def run_job():
    """Trigger the full job search workflow."""
    from workflows.job_search_graph import run_workflow
    logger.info(f"Scheduled run starting at {_now_cst()}")
    try:
        state = asyncio.run(run_workflow())
        logger.info(
            f"Run complete — {len(state['scored_jobs'])} jobs found, "
            f"digest_sent={state['digest_sent']}, "
            f"errors={state['errors']}"
        )
    except Exception as e:
        logger.error(f"Workflow failed: {e}", exc_info=True)


def main():
    console.print(
        "[bold green]BaluAgent Scheduler[/] — daily at [bold]9:00 AM CST[/]",
        style="bold",
    )
    console.print(f"Current time: {_now_cst()}")

    # Schedule at 09:00 CST every day
    schedule.every().day.at("09:00", "America/Chicago").do(run_job)

    # Show next run time
    next_run = schedule.next_run()
    if next_run:
        console.print(f"Next run scheduled: {next_run.astimezone(CST).strftime('%Y-%m-%d %H:%M:%S CST')}")

    console.print("Scheduler running... (Ctrl+C to stop)\n")

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
