"""Resume Tailor Agent — customizes resume bullets for a specific job."""
import json
import logging
from typing import Any

from langchain_openai import ChatOpenAI
from langchain.prompts import ChatPromptTemplate
from langchain.schema import HumanMessage, SystemMessage

logger = logging.getLogger(__name__)

TAILOR_PROMPT = ChatPromptTemplate.from_messages([
    SystemMessage(content="""You are an expert SRE/Platform engineer resume writer.
Given a job description and base resume bullets, rewrite the top 5 most relevant bullets
to mirror the job's language and keywords while keeping achievements truthful.

Return JSON:
{
  "tailored_bullets": ["...", "...", "...", "...", "..."],
  "keywords_added": ["..."],
  "ats_score_estimate": 0-100
}"""),
    HumanMessage(content="Job Description:\n{job_description}\n\nBase Resume:\n{base_resume}")
])

BASE_RESUME = """
- Reduced MTTD from 15min to 2min by building Datadog + LangChain anomaly detection pipeline
- Managed 200+ node EKS cluster across 3 AWS regions with 99.99% uptime SLA
- Built Terraform modules for VPC, EKS, RDS reducing provisioning time from 4hr to 20min
- Implemented LangGraph multi-agent SRE workflows automating 80% of tier-1 incidents
- Cut AWS costs from $8K to $3K/mo via rightsizing, spot instances, and S3 lifecycle policies
- Deployed production RAG system on Kubernetes serving 10K+ daily requests at p99 < 200ms
- Authored 40+ Terraform modules used across 6 engineering teams
- Built Prometheus + Grafana dashboards tracking 500+ SLIs/SLOs
- Led blameless postmortems reducing repeat incidents by 60%
- Mentored 5 junior SREs, establishing team runbook culture
"""


class ResumeTailorAgent:
    """Tailors resume content for specific job postings."""

    def __init__(self, llm: ChatOpenAI):
        self.llm = llm
        self.chain = TAILOR_PROMPT | llm

    async def tailor(self, job: dict) -> dict:
        """Generate tailored resume bullets for a job."""
        jd = f"""
Title: {job.get('jobtitle', job.get('title', ''))}
Company: {job.get('company', '')}
Description: {job.get('snippet', job.get('description', ''))[:1500]}
"""
        try:
            response = await self.chain.ainvoke({
                "job_description": jd,
                "base_resume": BASE_RESUME,
            })
            result = json.loads(response.content)
            return {
                "job_title": job.get("jobtitle", ""),
                "company": job.get("company", ""),
                "tailored_bullets": result.get("tailored_bullets", []),
                "keywords_added": result.get("keywords_added", []),
                "ats_score_estimate": result.get("ats_score_estimate", 0),
            }
        except Exception as e:
            logger.error(f"Tailoring failed: {e}")
            return {"error": str(e)}

    def get_state(self) -> dict[str, Any]:
        return {"agent": "ResumeTailorAgent"}
