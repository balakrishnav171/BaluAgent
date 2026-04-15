"""LangGraph multi-agent workflow for job search automation."""
import logging
import uuid
from datetime import datetime
from typing import TypedDict, Annotated
import operator

from langgraph.graph import StateGraph, END

from agents.job_scanner import JobScannerAgent
from agents.email_digest import EmailDigestAgent
from agents.resume_tailor import ResumeTailorAgent
from config.settings import settings

logger = logging.getLogger(__name__)


def _build_llm():
    """Build LLM — Ollama preferred, OpenAI fallback."""
    try:
        from langchain_community.chat_models import ChatOllama
        llm = ChatOllama(
            model=settings.model_name,
            base_url=settings.ollama_base_url,
            temperature=0.1,
            format="json",
        )
        logger.info(f"Workflow using Ollama: {settings.model_name}")
        return llm
    except Exception:
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model="gpt-4o", temperature=0.1, api_key=settings.openai_api_key)


class AgentState(TypedDict):
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
    """Build the LangGraph state machine."""

    async def scan_jobs(state: AgentState) -> AgentState:
        logger.info(f"[{state['run_id']}] Scanning jobs...")
        try:
            scored = await scanner.scan(state["roles"], state["locations"])
            return {**state, "scored_jobs": scored, "top_jobs": scored[:10]}
        except Exception as e:
            logger.error(f"Scan failed: {e}")
            return {**state, "errors": [str(e)], "scored_jobs": [], "top_jobs": []}

    async def tailor_resumes(state: AgentState) -> AgentState:
        logger.info(f"[{state['run_id']}] Tailoring top {len(state['top_jobs'][:3])} resumes...")
        tailored = []
        for job in state["top_jobs"][:3]:
            result = await tailor.tailor(job)
            tailored.append(result)
        return {**state, "tailored_resumes": tailored}

    def send_digest(state: AgentState) -> AgentState:
        logger.info(f"[{state['run_id']}] Sending digest ({len(state['scored_jobs'])} jobs)...")
        sent = digest.send(state["scored_jobs"])
        return {**state, "digest_sent": sent, "completed_at": datetime.utcnow().isoformat()}

    def should_tailor(state: AgentState) -> str:
        return "tailor" if state.get("top_jobs") else "digest"

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
    llm = _build_llm()
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

    logger.info(f"Starting BaluAgent run_id={initial_state['run_id']}")
    final_state = await workflow.ainvoke(initial_state)
    logger.info(
        f"Complete: {len(final_state['scored_jobs'])} jobs, digest_sent={final_state['digest_sent']}"
    )
    return final_state
