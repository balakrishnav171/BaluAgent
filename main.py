"""BaluAgent — Agentic Job Search Automation. Entry point."""
import asyncio
import logging
import sys

import click
import schedule
import time
from rich.console import Console
from rich.table import Table
from rich.logging import RichHandler

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[RichHandler(rich_tracebacks=True)],
)
logger = logging.getLogger("BaluAgent")
console = Console()


@click.group()
def cli():
    """BaluAgent — Multi-agent job search automation powered by LangChain + LangGraph."""
    pass


@cli.command()
def run():
    """Run a single job search workflow now."""
    from workflows.job_search_graph import run_workflow

    console.print("[bold green]BaluAgent[/] starting single run...", style="bold")

    async def _run():
        state = await run_workflow()
        _print_summary(state)

    asyncio.run(_run())


@cli.command()
@click.option("--interval-hours", default=24, help="Hours between scans")
def schedule_daemon(interval_hours: int):
    """Run BaluAgent on a schedule (daemon mode)."""
    from workflows.job_search_graph import run_workflow

    console.print(
        f"[bold green]BaluAgent[/] daemon started — scanning every {interval_hours}h",
        style="bold",
    )

    def _run_sync():
        asyncio.run(run_workflow())

    schedule.every(interval_hours).hours.do(_run_sync)
    _run_sync()  # Run immediately on start

    while True:
        schedule.run_pending()
        time.sleep(60)


@cli.command()
def serve_mcp():
    """Start the MCP server."""
    from tools.mcp_server import start
    console.print("[bold blue]Starting MCP server...[/]")
    start()


@cli.command()
def status():
    """Show BaluAgent configuration status."""
    from config.settings import settings

    table = Table(title="BaluAgent Configuration")
    table.add_column("Setting", style="cyan")
    table.add_column("Value", style="green")
    table.add_column("Status", style="bold")

    checks = [
        ("OpenAI API Key", "***" if settings.openai_api_key else "NOT SET",
         "✓" if settings.openai_api_key else "✗"),
        ("SMTP User", settings.smtp_user or "NOT SET",
         "✓" if settings.smtp_user else "⚠"),
        ("Digest Recipient", settings.digest_recipient or "NOT SET",
         "✓" if settings.digest_recipient else "⚠"),
        ("Model", settings.model_name, "✓"),
        ("Min Match Score", str(settings.min_match_score), "✓"),
        ("Target Roles", ", ".join(settings.target_roles[:2]) + "...", "✓"),
        ("Scan Interval", f"{settings.job_scan_interval_hours}h", "✓"),
    ]

    for setting, value, status in checks:
        table.add_row(setting, value, status)

    console.print(table)


def _print_summary(state: dict):
    """Print workflow results to terminal."""
    table = Table(title=f"BaluAgent Run Summary — {state.get('run_id', 'unknown')}")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("Jobs Scanned", str(len(state.get("scored_jobs", []))))
    table.add_row("Top Matches", str(len(state.get("top_jobs", []))))
    table.add_row("Resumes Tailored", str(len(state.get("tailored_resumes", []))))
    table.add_row("Digest Sent", "✓" if state.get("digest_sent") else "✗")
    table.add_row("Errors", str(len(state.get("errors", []))))
    table.add_row("Started", state.get("started_at", ""))
    table.add_row("Completed", state.get("completed_at", ""))

    console.print(table)

    if state.get("top_jobs"):
        job_table = Table(title="Top Job Matches")
        job_table.add_column("Score", style="green")
        job_table.add_column("Title", style="cyan")
        job_table.add_column("Company")
        job_table.add_column("Location")

        for job in state["top_jobs"][:10]:
            job_table.add_row(
                f"{int(job.get('match_score', 0) * 100)}%",
                job.get("jobtitle", job.get("title", "N/A")),
                job.get("company", "N/A"),
                job.get("formattedLocation", "Remote"),
            )
        console.print(job_table)


if __name__ == "__main__":
    cli()
