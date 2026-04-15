"""Job Scanner Agent — searches LinkedIn, Indeed, Dice, Glassdoor, ZipRecruiter + Remotive."""
import asyncio
import json
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

PORTALS = ["linkedin", "indeed", "glassdoor", "zip_recruiter"]
# Dice requires separate handling via jobspy
DICE_PORTAL = ["dice"]


def _build_llm():
    from config.settings import settings
    try:
        from langchain_community.chat_models import ChatOllama
        llm = ChatOllama(
            model=settings.model_name,
            base_url=settings.ollama_base_url,
            temperature=0.1,
            format="json",
        )
        logger.info(f"Using Ollama model: {settings.model_name}")
        return llm
    except Exception as e:
        logger.warning(f"Ollama unavailable ({e}), falling back to OpenAI")
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model="gpt-4o", temperature=0.1, api_key=settings.openai_api_key)


SCORING_SYSTEM = """You are a job match scorer. Score how well a job matches this candidate:
Skills: Kubernetes, Terraform, AWS, Python, LangChain, DevOps, SRE, CI/CD, Prometheus, Grafana, AI/ML
Experience: 8 years SRE/Platform/DevOps/AI Engineering
Visa: H1B (needs sponsorship or h1b-friendly employer; cannot work on jobs requiring US citizenship, green card, or security clearance)

Return ONLY valid JSON:
{"score": 0.85, "reason": "Strong match because...", "highlights": ["skill1", "skill2"]}
score must be 0.0 to 1.0"""

# Keywords that disqualify a job for H1B candidates
_H1B_DISQUALIFY = [
    "us citizen", "u.s. citizen", "united states citizen",
    "must be a citizen", "citizenship required", "citizens only",
    "green card", "permanent resident", "gc only", "gc required",
    "security clearance", "secret clearance", "top secret", "ts/sci",
    "ts clearance", "dod clearance", "public trust clearance",
    "no sponsorship", "not eligible for sponsorship",
    "sponsorship not available", "cannot sponsor",
    "we are unable to sponsor", "not able to sponsor",
    "authorization to work in the us without sponsorship",
    "must be authorized to work without sponsorship",
]

# Keywords that indicate H1B-friendly or sponsorship offered
_H1B_FRIENDLY = [
    "h1b", "h-1b", "visa sponsorship", "sponsorship available",
    "will sponsor", "open to sponsorship", "h1b transfer",
]


def _is_h1b_eligible(job: dict) -> tuple[bool, str]:
    """Return (eligible, reason). Filters out citizenship/clearance/no-sponsorship jobs."""
    text = (
        job.get("jobtitle", "") + " " +
        job.get("snippet", "") + " " +
        job.get("company", "")
    ).lower()

    for phrase in _H1B_DISQUALIFY:
        if phrase in text:
            return False, f"Disqualified: '{phrase}' found in job description"

    h1b_hint = any(phrase in text for phrase in _H1B_FRIENDLY)
    return True, "H1B friendly" if h1b_hint else "No explicit sponsorship mention"


def _fetch_jobspy(role: str, portal: str, hours_old: int = 24, results: int = 15) -> list[dict]:
    """Fetch jobs from a single portal via jobspy."""
    try:
        from jobspy import scrape_jobs
        df = scrape_jobs(
            site_name=[portal],
            search_term=role,
            location="Remote",
            results_wanted=results,
            hours_old=hours_old,
            country_indeed="USA",
            verbose=0,
        )
        if df is None or df.empty:
            return []

        jobs = []
        for _, row in df.iterrows():
            jobs.append({
                "jobtitle":          str(row.get("title", "")),
                "company":           str(row.get("company", "")),
                "formattedLocation": str(row.get("location", "Remote")),
                "snippet":           str(row.get("description", ""))[:500] if row.get("description") else "",
                "url":               str(row.get("job_url", "")),
                "source":            portal,
                "date":              str(row.get("date_posted", "")),
            })
        logger.info(f"  {portal}: found {len(jobs)} jobs for '{role}'")
        return jobs
    except Exception as e:
        logger.warning(f"  {portal} scrape failed for '{role}': {e}")
        return []


async def _fetch_remotive(role: str, client: httpx.AsyncClient) -> list[dict]:
    """Fetch from Remotive free API."""
    try:
        resp = await client.get(
            "https://remotive.com/api/remote-jobs",
            params={"search": role, "limit": 20},
            timeout=15.0,
        )
        if resp.status_code == 200:
            jobs = resp.json().get("jobs", [])
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            results = []
            for j in jobs:
                pub = j.get("publication_date", "")
                try:
                    dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt < cutoff:
                        continue
                except Exception:
                    pass
                results.append({
                    "jobtitle":          j.get("title", ""),
                    "company":           j.get("company_name", ""),
                    "formattedLocation": "Remote",
                    "snippet":           re.sub(r'<[^>]+>', '', j.get("description", ""))[:500],
                    "url":               j.get("url", ""),
                    "source":            "remotive",
                    "date":              pub,
                })
            return results
    except Exception as e:
        logger.warning(f"Remotive fetch failed for '{role}': {e}")
    return []


class JobScannerAgent:
    """Searches LinkedIn, Indeed, Glassdoor, Dice, ZipRecruiter, Remotive."""

    def __init__(self, llm=None, min_score: float = 0.3):
        self.llm = llm or _build_llm()
        self.min_score = min_score

    def _score_job_sync(self, job: dict) -> dict:
        prompt = (
            f"{SCORING_SYSTEM}\n\nJob:\n"
            f"Title: {job.get('jobtitle', '')}\n"
            f"Company: {job.get('company', '')}\n"
            f"Location: {job.get('formattedLocation', '')}\n"
            f"Description: {job.get('snippet', '')[:400]}"
        )
        try:
            from langchain.schema import HumanMessage
            response = self.llm.invoke([HumanMessage(content=prompt)])
            raw = response.content.strip()
            match = re.search(r'\{.*\}', raw, re.DOTALL)
            result = json.loads(match.group() if match else raw)
            job["match_score"] = float(result.get("score", 0.5))
            job["match_reason"] = result.get("reason", "")
            job["highlights"] = result.get("highlights", [])
        except Exception as e:
            logger.warning(f"Scoring failed for '{job.get('jobtitle','?')}': {e}")
            text = (job.get("jobtitle", "") + " " + job.get("snippet", "")).lower()
            kw = ["sre","devops","platform","kubernetes","terraform","aws","python",
                  "reliability","infrastructure","cloud","ai","langchain","agentic"]
            hits = [k for k in kw if k in text]
            job["match_score"] = min(0.4 + len(hits) * 0.05, 0.95)
            job["match_reason"] = f"Keyword match: {', '.join(hits)}" if hits else "General match"
            job["highlights"] = hits[:4]
        job["missing_skills"] = []
        return job

    async def scan(self, roles: list[str], locations: list[str]) -> list[dict]:
        logger.info(f"Scanning {len(roles)} roles across LinkedIn, Indeed, Glassdoor, Dice, ZipRecruiter, Remotive")
        raw_jobs: list[dict] = []

        # 1. jobspy portals — run in thread pool (blocking scraper)
        loop = asyncio.get_event_loop()
        jobspy_tasks = []
        for role in roles:
            for portal in PORTALS + DICE_PORTAL:
                jobspy_tasks.append(
                    loop.run_in_executor(None, _fetch_jobspy, role, portal, 24, 10)
                )

        # 2. Remotive async
        async with httpx.AsyncClient(headers={"User-Agent": "BaluAgent/1.0"}) as client:
            remotive_tasks = [_fetch_remotive(role, client) for role in roles]
            all_results = await asyncio.gather(
                *jobspy_tasks, *remotive_tasks, return_exceptions=True
            )

        for r in all_results:
            if isinstance(r, list):
                raw_jobs.extend(r)

        # Deduplicate by URL
        seen: set[str] = set()
        unique: list[dict] = []
        for job in raw_jobs:
            url = job.get("url", "")
            if url and url not in seen:
                seen.add(url)
                unique.append(job)

        logger.info(f"Total unique jobs fetched: {len(unique)} — filtering for H1B eligibility...")

        # H1B visa filter — drop citizenship/clearance/no-sponsorship jobs
        h1b_eligible = []
        h1b_dropped = 0
        for job in unique:
            eligible, reason = _is_h1b_eligible(job)
            if eligible:
                job["visa_note"] = reason
                h1b_eligible.append(job)
            else:
                h1b_dropped += 1
                logger.debug(f"  Dropped (H1B filter): {job.get('jobtitle','?')} @ {job.get('company','?')} — {reason}")

        logger.info(f"H1B filter: kept {len(h1b_eligible)}, dropped {h1b_dropped} (citizenship/clearance/no-sponsorship)")

        logger.info(f"Scoring {len(h1b_eligible)} H1B-eligible jobs...")

        # Score all jobs
        scored = [self._score_job_sync(j) for j in h1b_eligible]
        qualified = [j for j in scored if j.get("match_score", 0) >= self.min_score]
        qualified.sort(key=lambda x: x.get("match_score", 0), reverse=True)

        # Log portal breakdown
        by_source: dict[str, int] = {}
        for j in qualified:
            s = j.get("source", "unknown")
            by_source[s] = by_source.get(s, 0) + 1
        logger.info(f"Qualified {len(qualified)}/{len(unique)} jobs | by portal: {by_source}")

        return qualified

    def get_state(self) -> dict[str, Any]:
        return {"agent": "JobScannerAgent", "min_score": self.min_score,
                "portals": PORTALS + DICE_PORTAL + ["remotive"]}
