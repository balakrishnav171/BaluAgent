"""LangGraph multi-agent workflow for job search automation."""
import logging
from datetime import datetime
from typing import TypedDict, Annotated
import operator

from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, END

from agents.job_scanner import JobScannerAgent
from agents.email_digest import EmailDigestAgent
from agents.resume_tailor import ResumeTailorAgent
from config.settings import settings

logger = logging.getLogger(__name__)


class AgentState(TypedDict):
    """Shared state across all agents in the workflow."""
    roles: list[str]
    locations: list[str]
    raw_jobs: list[dict]
    scored_jobs: list[dict]
    top_jobs: list[dict]
    tailored_resumes: list[dict]
    digest_sent: bool
    errors: Annotated[list[str], operator.add]
    run_id: str
    started_at: str
    completed_at: str


def build_workflow(
    scanner: JobScannerAgent,
    digest: EmailDigestAgent,
    tailor: ResumeTailorAgent,
) -> StateGraph:
    """Build the LangGraph state machine for job search."""

    async def scan_jobs(state: AgentState) -> AgentState:
        """Node: Fetch and score jobs from all sources."""
        logger.info(f"[{state['run_id']}] Scanning jobs...")
        try:
            scored = await scanner.scan(state["roles"], state["locations"])
            return {
                **state,
                "scored_jobs": scored,
                "top_jobs": scored[:10],
            }
        except Exception as e:
            logger.error(f"Scan failed: {e}")
            return {**state, "errors": [str(e)], "scored_jobs": [], "top_jobs": []}

    async def tailor_resumes(state: AgentState) -> AgentState:
        """Node: Generate tailored resume bullets for top jobs."""
        logger.info(f"[{state['run_id']}] Tailoring resumes for {len(state['top_jobs'])} jobs...")
        tailored = []
        for job in state["top_jobs"][:5]:  # tailor top 5
            result = await tailor.tailor(job)
            tailored.append(result)
        return {**state, "tailored_resumes": tailored}

    def send_digest(state: AgentState) -> AgentState:
        """Node: Send email digest."""
        logger.info(f"[{state['run_id']}] Sending digest with {len(state['scored_jobs'])} jobs...")
        sent = digest.send(state["scored_jobs"])
        return {
            **state,
            "digest_sent": sent,
            "completed_at": datetime.utcnow().isoformat(),
        }

    def should_tailor(state: AgentState) -> str:
        """Conditional edge: skip tailoring if no good matches."""
        if len(state.get("top_jobs", [])) > 0:
            return "tailor"
        return "digest"

    # Build graph
    graph = StateGraph(AgentState)
    graph.add_node("scan", scan_jobs)
    graph.add_node("tailor", tailor_resumes)
    graph.add_node("digest", send_digest)

    graph.set_entry_point("scan")
    graph.add_conditional_edges("scan", should_tailor, {"tailor": "tailor", "digest": "digest"})
    graph.add_edge("tailor", "digest")
    graph.add_edge("digest", END)

    return graph.compile()


async def run_workflow(run_id: str | None = None) -> AgentState:
    """Execute the full multi-agent job search workflow."""
    import uuid

    llm = ChatOpenAI(
        model=settings.model_name,
        temperature=0.1,
        api_key=settings.openai_api_key,
    )

    scanner = JobScannerAgent(llm=llm, min_score=settings.min_match_score)
    digest = EmailDigestAgent(
        smtp_host=settings.smtp_host,
        smtp_port=settings.smtp_port,
        smtp_user=settings.smtp_user,
        smtp_password=settings.smtp_password,
        recipient=settings.digest_recipient,
    )
    tailor = ResumeTailorAgent(llm=llm)

    workflow = build_workflow(scanner, digest, tailor)

    initial_state: AgentState = {
        "roles": settings.target_roles,
        "locations": settings.target_locations,
        "raw_jobs": [],
        "scored_jobs": [],
        "top_jobs": [],
        "tailored_resumes": [],
        "digest_sent": False,
        "errors": [],
        "run_id": run_id or str(uuid.uuid4())[:8],
        "started_at": datetime.utcnow().isoformat(),
        "completed_at": "",
    }

    logger.info(f"Starting BaluAgent workflow run_id={initial_state['run_id']}")
    final_state = await workflow.ainvoke(initial_state)
    logger.info(
        f"Workflow complete: {len(final_state['scored_jobs'])} jobs, "
        f"digest_sent={final_state['digest_sent']}"
    )
    return final_state
