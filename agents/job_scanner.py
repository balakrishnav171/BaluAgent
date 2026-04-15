"""Job Scanner Agent — discovers and filters job postings."""
import asyncio
import json
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)


def _build_llm():
    """Build LLM — Ollama if available, else OpenAI."""
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


SCORING_SYSTEM = """You are a job match scorer. Score how well a job matches this candidate profile:
Skills: Kubernetes, Terraform, AWS, Python, LangChain, DevOps, SRE, CI/CD, Prometheus, Grafana
Experience: 8 years SRE/Platform/DevOps

Return ONLY valid JSON in this exact format (no other text):
{"score": 0.85, "reason": "Strong match because...", "highlights": ["skill1", "skill2"]}

score must be a number between 0.0 and 1.0"""


class JobScannerAgent:
    """Scans multiple job boards and scores relevance using LLM."""

    def __init__(self, llm=None, min_score: float = 0.3):
        self.llm = llm or _build_llm()
        self.min_score = min_score

    def _is_last_24h(self, date_str: str) -> bool:
        """Return True if date_str is within the last 24 hours."""
        if not date_str:
            return True  # include if no date
        try:
            # Remotive format: "2024-04-14T10:00:00"
            pub = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=timezone.utc)
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            return pub >= cutoff
        except Exception:
            return True  # include if unparseable

    async def _fetch_remotive_jobs(self, role: str, client: httpx.AsyncClient) -> list[dict]:
        """Fetch remote jobs from Remotive API (free, no key needed)."""
        try:
            resp = await client.get(
                "https://remotive.com/api/remote-jobs",
                params={"search": role, "limit": 20},
                timeout=15.0,
            )
            if resp.status_code == 200:
                jobs = resp.json().get("jobs", [])
                results = []
                skipped = 0
                for j in jobs:
                    pub_date = j.get("publication_date", "")
                    if not self._is_last_24h(pub_date):
                        skipped += 1
                        continue
                    results.append({
                        "jobtitle": j.get("title", ""),
                        "company": j.get("company_name", ""),
                        "formattedLocation": "Remote",
                        "snippet": re.sub(r'<[^>]+>', '', j.get("description", ""))[:400],
                        "url": j.get("url", ""),
                        "source": "remotive",
                        "date": pub_date,
                    })
                if skipped:
                    logger.info(f"  '{role}': skipped {skipped} jobs older than 24h")
                return results
        except Exception as e:
            logger.warning(f"Remotive fetch failed for '{role}': {e}")
        return []

    def _score_job_sync(self, job: dict) -> dict:
        """Score a job using LLM (sync call for Ollama compatibility)."""
        job_text = (
            f"Title: {job.get('jobtitle', '')}\n"
            f"Company: {job.get('company', '')}\n"
            f"Description: {job.get('snippet', '')[:500]}"
        )
        prompt = f"{SCORING_SYSTEM}\n\nJob:\n{job_text}"

        try:
            from langchain.schema import HumanMessage
            response = self.llm.invoke([HumanMessage(content=prompt)])
            raw = response.content.strip()

            # Extract JSON robustly
            match = re.search(r'\{.*?\}', raw, re.DOTALL)
            if match:
                result = json.loads(match.group())
            else:
                result = json.loads(raw)

            job["match_score"] = float(result.get("score", 0.5))
            job["match_reason"] = result.get("reason", "")
            job["highlights"] = result.get("highlights", [])

        except Exception as e:
            logger.warning(f"Scoring failed for '{job.get('jobtitle', '?')}': {e}")
            # Fallback: keyword-based scoring
            text = (job.get("jobtitle", "") + " " + job.get("snippet", "")).lower()
            keywords = ["sre", "devops", "platform", "kubernetes", "terraform", "aws",
                        "python", "ci/cd", "reliability", "infrastructure", "cloud"]
            hits = [k for k in keywords if k in text]
            job["match_score"] = min(0.5 + len(hits) * 0.05, 0.95)
            job["match_reason"] = f"Keyword match: {', '.join(hits)}" if hits else "General match"
            job["highlights"] = hits[:4]

        job["missing_skills"] = []
        return job

    async def scan(self, roles: list[str], locations: list[str]) -> list[dict]:
        """Fetch jobs, score them, return sorted results above min_score."""
        logger.info(f"Scanning jobs for roles: {roles}")
        raw_jobs: list[dict] = []

        async with httpx.AsyncClient(headers={"User-Agent": "BaluAgent/1.0"}) as client:
            tasks = [self._fetch_remotive_jobs(role, client) for role in roles]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, list):
                    raw_jobs.extend(r)

        # Deduplicate
        seen: set[str] = set()
        unique: list[dict] = []
        for job in raw_jobs:
            url = job.get("url", "")
            if url and url not in seen:
                seen.add(url)
                unique.append(job)

        logger.info(f"Fetched {len(unique)} unique jobs, scoring with {self.llm.__class__.__name__}...")

        # Score each job (sync to avoid Ollama async issues)
        scored = [self._score_job_sync(j) for j in unique]

        qualified = [j for j in scored if j.get("match_score", 0) >= self.min_score]
        qualified.sort(key=lambda x: x.get("match_score", 0), reverse=True)

        logger.info(f"Done: {len(qualified)}/{len(unique)} jobs qualify (threshold={self.min_score})")
        return qualified

    def get_state(self) -> dict[str, Any]:
        return {
            "agent": "JobScannerAgent",
            "min_score": self.min_score,
            "last_run": datetime.utcnow().isoformat(),
        }
