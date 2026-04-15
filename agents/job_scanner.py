"""Job Scanner Agent — discovers and filters job postings."""
import asyncio
import json
import logging
from datetime import datetime
from typing import Any

import httpx
from langchain_openai import ChatOpenAI
from langchain.prompts import ChatPromptTemplate
from langchain.schema import HumanMessage, SystemMessage
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)


SCORING_PROMPT = ChatPromptTemplate.from_messages([
    SystemMessage(content="""You are an expert technical recruiter. Score the job posting match for a
Senior SRE/Platform/AI Engineer with these skills:
- 8+ years SRE/DevOps/Platform Engineering
- Kubernetes, Terraform, AWS/GCP/Azure
- Python, Go
- LangChain, LangGraph, AI/ML Ops
- Datadog, Prometheus, Grafana
- CI/CD, GitHub Actions

Return JSON: {{"score": 0.0-1.0, "reason": "...", "highlights": ["..."], "missing": ["..."]}}"""),
    HumanMessage(content="Job posting:\n{job_text}")
])


class JobScannerAgent:
    """Scans multiple job boards and scores relevance using LLM."""

    def __init__(self, llm: ChatOpenAI, min_score: float = 0.75):
        self.llm = llm
        self.min_score = min_score
        self.scoring_chain = SCORING_PROMPT | llm

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def _fetch_indeed_jobs(
        self, role: str, location: str, client: httpx.AsyncClient
    ) -> list[dict]:
        """Fetch jobs from Indeed-compatible API."""
        params = {
            "q": role,
            "l": location,
            "sort": "date",
            "limit": 25,
            "fromage": 1,  # last 24 hours
        }
        try:
            resp = await client.get(
                "https://api.indeed.com/ads/apisearch",
                params=params,
                timeout=10.0,
            )
            if resp.status_code == 200:
                return resp.json().get("results", [])
        except Exception as e:
            logger.warning(f"Indeed fetch failed for {role}/{location}: {e}")
        return []

    async def _fetch_remotive_jobs(self, role: str, client: httpx.AsyncClient) -> list[dict]:
        """Fetch remote jobs from Remotive API (free, no key needed)."""
        try:
            resp = await client.get(
                "https://remotive.com/api/remote-jobs",
                params={"search": role, "limit": 20},
                timeout=10.0,
            )
            if resp.status_code == 200:
                jobs = resp.json().get("jobs", [])
                return [
                    {
                        "jobtitle": j.get("title", ""),
                        "company": j.get("company_name", ""),
                        "formattedLocation": "Remote",
                        "snippet": j.get("description", "")[:500],
                        "url": j.get("url", ""),
                        "source": "remotive",
                        "date": j.get("publication_date", ""),
                    }
                    for j in jobs
                ]
        except Exception as e:
            logger.warning(f"Remotive fetch failed for {role}: {e}")
        return []

    async def score_job(self, job: dict) -> dict:
        """Score a single job posting using LLM."""
        job_text = f"""
Title: {job.get('jobtitle', job.get('title', 'N/A'))}
Company: {job.get('company', 'N/A')}
Location: {job.get('formattedLocation', 'N/A')}
Description: {job.get('snippet', job.get('description', ''))[:800]}
"""
        try:
            response = await self.scoring_chain.ainvoke({"job_text": job_text})
            result = json.loads(response.content)
            job["match_score"] = result.get("score", 0.0)
            job["match_reason"] = result.get("reason", "")
            job["highlights"] = result.get("highlights", [])
            job["missing_skills"] = result.get("missing", [])
        except Exception as e:
            logger.warning(f"Scoring failed for {job.get('jobtitle', 'unknown')}: {e}")
            job["match_score"] = 0.0
            job["match_reason"] = "Scoring unavailable"
            job["highlights"] = []
            job["missing_skills"] = []
        return job

    async def scan(
        self, roles: list[str], locations: list[str]
    ) -> list[dict]:
        """
        Main scan entry point. Fetches from all sources, scores, filters.
        Returns jobs above min_score, sorted by score descending.
        """
        logger.info(f"Starting job scan: {len(roles)} roles × {len(locations)} locations")
        raw_jobs: list[dict] = []

        async with httpx.AsyncClient(headers={"User-Agent": "BaluAgent/1.0"}) as client:
            tasks = []
            for role in roles:
                tasks.append(self._fetch_remotive_jobs(role, client))
                for loc in locations:
                    tasks.append(self._fetch_indeed_jobs(role, loc, client))

            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, list):
                    raw_jobs.extend(r)

        # Deduplicate by URL
        seen: set[str] = set()
        unique_jobs: list[dict] = []
        for job in raw_jobs:
            url = job.get("url", job.get("jobkey", ""))
            if url and url not in seen:
                seen.add(url)
                unique_jobs.append(job)

        logger.info(f"Fetched {len(unique_jobs)} unique jobs, scoring...")

        # Score concurrently (batch of 10)
        scored: list[dict] = []
        batch_size = 10
        for i in range(0, len(unique_jobs), batch_size):
            batch = unique_jobs[i : i + batch_size]
            batch_results = await asyncio.gather(*[self.score_job(j) for j in batch])
            scored.extend(batch_results)

        # Filter & sort
        qualified = [j for j in scored if j.get("match_score", 0) >= self.min_score]
        qualified.sort(key=lambda x: x.get("match_score", 0), reverse=True)

        logger.info(
            f"Scan complete: {len(qualified)}/{len(unique_jobs)} jobs meet threshold {self.min_score}"
        )
        return qualified

    def get_state(self) -> dict[str, Any]:
        return {
            "agent": "JobScannerAgent",
            "min_score": self.min_score,
            "last_run": datetime.utcnow().isoformat(),
        }
