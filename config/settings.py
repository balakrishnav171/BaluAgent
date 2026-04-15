"""Central configuration for BaluAgent."""
from pydantic_settings import BaseSettings
from pydantic import Field
from typing import List


class Settings(BaseSettings):
    # LLM — Ollama or OpenAI
    openai_api_key: str = Field(default="dummy", env="OPENAI_API_KEY")
    ollama_base_url: str = Field(default="http://localhost:11434", env="OLLAMA_BASE_URL")
    model_name: str = Field(default="orca-mini", env="MODEL_NAME")
    use_ollama: bool = Field(default=True, env="USE_OLLAMA")

    # LangSmith
    langchain_api_key: str = Field(default="", env="LANGCHAIN_API_KEY")
    langchain_tracing_v2: bool = Field(default=False, env="LANGCHAIN_TRACING_V2")
    langchain_project: str = Field(default="BaluAgent", env="LANGCHAIN_PROJECT")

    # Email
    smtp_host: str = Field(default="smtp.gmail.com", env="SMTP_HOST")
    smtp_port: int = Field(default=587, env="SMTP_PORT")
    smtp_user: str = Field(default="", env="SMTP_USER")
    smtp_password: str = Field(default="", env="SMTP_PASSWORD")
    digest_recipient: str = Field(default="", env="DIGEST_RECIPIENT")

    # Job Search
    max_jobs_per_run: int = Field(default=20, env="MAX_JOBS_PER_RUN")
    min_match_score: float = Field(default=0.3, env="MIN_MATCH_SCORE")
    target_roles: List[str] = Field(
        default=["Senior SRE", "Platform Engineer", "DevOps Engineer"],
        env="TARGET_ROLES"
    )
    target_locations: List[str] = Field(
        default=["Remote"],
        env="TARGET_LOCATIONS"
    )
    job_scan_interval_hours: int = Field(default=24, env="JOB_SCAN_INTERVAL_HOURS")

    # MCP
    mcp_server_port: int = Field(default=8765, env="MCP_SERVER_PORT")
    mcp_secret_key: str = Field(default="", env="MCP_SECRET_KEY")

    # A2A
    a2a_agent_id: str = Field(default="balu-agent-001", env="A2A_AGENT_ID")
    a2a_coordinator_url: str = Field(default="http://localhost:9000", env="A2A_COORDINATOR_URL")

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "protected_namespaces": ("settings_",),
    }


settings = Settings()
