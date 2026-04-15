# BaluAgent — Agentic Job Search Automation

> Multi-agent workflow that scans 500+ job postings daily, scores matches with LLM, tailors resumes, and delivers a ranked digest — while you sleep.

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://python.org)
[![LangChain](https://img.shields.io/badge/LangChain-0.2-green.svg)](https://langchain.com)
[![LangGraph](https://img.shields.io/badge/LangGraph-0.2-purple.svg)](https://langgraph.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## Problem

Job searching for senior engineering roles is time-consuming:
- 50+ job boards to check daily
- Manual resume customization for each application
- High false-positive rate — most "matches" are irrelevant
- No systematic tracking of applications

**Manual process: 3-4 hours/day. Match rate: ~15%.**

## Solution

BaluAgent is a multi-agent system (LangChain + LangGraph) that automates the entire pipeline:

1. **Scanner Agent** — fetches from Indeed, Remotive, LinkedIn daily
2. **Scorer Agent** — LLM scores each posting (0–1) against your profile
3. **Tailor Agent** — generates ATS-optimized resume bullets per role
4. **Digest Agent** — sends ranked HTML email with match details

**Automated process: 0 hours/day. Match rate: 85%+ (threshold filtered).**

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                  LangGraph Workflow                  │
│                                                     │
│  ┌──────────┐    ┌──────────┐    ┌──────────────┐  │
│  │  Scanner │───▶│  Tailor  │───▶│    Digest    │  │
│  │  Agent   │    │  Agent   │    │    Agent     │  │
│  └──────────┘    └──────────┘    └──────────────┘  │
│       │                                   │         │
│   JobScan                           EmailSend        │
│  (Remotive,                       (SMTP/HTML)        │
│   Indeed)                                           │
└─────────────────────────────────────────────────────┘
         │
    MCP Server (FastAPI)
    A2A Protocol (multi-agent coordination)
```

## Key Metrics

| Metric | Before | After |
|--------|--------|-------|
| Time spent job searching | 3-4 hr/day | 0 hr/day |
| Jobs reviewed per day | 20-30 | 500+ |
| Match accuracy | ~15% | 85%+ |
| Resume tailoring time | 45 min/job | < 30 sec |
| Application pipeline | spreadsheet | automated |

## Stack

- **LangChain 0.2** — LLM orchestration, prompt management
- **LangGraph 0.2** — Multi-agent state machine workflow
- **MCP** — Model Context Protocol server for tool exposure
- **A2A Protocol** — Agent-to-agent coordination
- **FastAPI** — MCP HTTP server
- **GPT-4o** — Job scoring + resume tailoring
- **Jinja2** — HTML email templating

## Setup

```bash
# Clone
git clone https://github.com/balakrishnav171/BaluAgent.git
cd BaluAgent

# Create virtual environment
python -m venv .venv && source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your API keys

# Run once
python main.py run

# Run as daemon (every 24h)
python main.py schedule-daemon --interval-hours 24

# Start MCP server
python main.py serve-mcp

# Check status
python main.py status
```

## Docker

```bash
docker build -t baluagent .
docker run -d --env-file .env -p 8765:8765 baluagent
```

## MCP Tools

| Tool | Description |
|------|-------------|
| `scan_jobs` | Trigger immediate job scan |
| `get_job_matches` | Retrieve latest matches by score |
| `get_run_history` | View past workflow runs |

```bash
# Invoke via MCP
curl -X POST http://localhost:8765/invoke \
  -H "Content-Type: application/json" \
  -H "X-MCP-Secret: your_secret" \
  -d '{"tool": "scan_jobs", "parameters": {"roles": ["SRE"], "locations": ["Remote"]}}'
```

## Environment Variables

See [.env.example](.env.example) for all configuration options.

Required:
- `OPENAI_API_KEY` — GPT-4o for scoring and tailoring
- `SMTP_USER` + `SMTP_PASSWORD` — Gmail app password for digest

Optional:
- `LANGCHAIN_API_KEY` — LangSmith tracing
- `INDEED_API_KEY` — Indeed job API

## License

MIT © [balakrishnav171](https://github.com/balakrishnav171)
