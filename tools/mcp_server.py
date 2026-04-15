"""MCP (Model Context Protocol) server exposing BaluAgent tools."""
import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
import uvicorn

from config.settings import settings

logger = logging.getLogger(__name__)
app = FastAPI(title="BaluAgent MCP Server", version="1.0.0")

# In-memory run history (use Redis/DB in production)
_run_history: list[dict] = []


class MCPToolRequest(BaseModel):
    tool: str
    parameters: dict[str, Any] = {}


class MCPToolResponse(BaseModel):
    tool: str
    result: Any
    timestamp: str
    success: bool
    error: str | None = None


def _auth(secret: str = Header(default="", alias="X-MCP-Secret")) -> None:
    if settings.mcp_secret_key and secret != settings.mcp_secret_key:
        raise HTTPException(status_code=401, detail="Invalid MCP secret")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "agent": "BaluAgent", "version": "1.0.0"}


@app.get("/tools")
async def list_tools() -> dict:
    """List available MCP tools."""
    return {
        "tools": [
            {
                "name": "scan_jobs",
                "description": "Trigger a job scan for given roles and locations",
                "parameters": {
                    "roles": "list[str]",
                    "locations": "list[str]",
                    "min_score": "float (0-1)",
                },
            },
            {
                "name": "get_run_history",
                "description": "Get history of past workflow runs",
                "parameters": {"limit": "int (default 10)"},
            },
            {
                "name": "get_job_matches",
                "description": "Get latest job matches above score threshold",
                "parameters": {"min_score": "float", "limit": "int"},
            },
        ]
    }


@app.post("/invoke", response_model=MCPToolResponse)
async def invoke_tool(request: MCPToolRequest) -> MCPToolResponse:
    """Invoke an MCP tool."""
    _auth()
    timestamp = datetime.utcnow().isoformat()

    try:
        if request.tool == "scan_jobs":
            # Lazy import to avoid circular deps
            from workflows.job_search_graph import run_workflow
            import uuid

            run_id = str(uuid.uuid4())[:8]
            # Fire and forget — return run_id immediately
            asyncio.create_task(run_workflow(run_id=run_id))
            result = {"run_id": run_id, "status": "started", "message": "Workflow triggered async"}

        elif request.tool == "get_run_history":
            limit = request.parameters.get("limit", 10)
            result = _run_history[-limit:]

        elif request.tool == "get_job_matches":
            min_score = request.parameters.get("min_score", 0.75)
            limit = request.parameters.get("limit", 20)
            if _run_history:
                last_run = _run_history[-1]
                jobs = last_run.get("scored_jobs", [])
                result = [j for j in jobs if j.get("match_score", 0) >= min_score][:limit]
            else:
                result = []

        else:
            raise HTTPException(status_code=400, detail=f"Unknown tool: {request.tool}")

        return MCPToolResponse(tool=request.tool, result=result, timestamp=timestamp, success=True)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Tool invocation failed: {e}")
        return MCPToolResponse(
            tool=request.tool, result=None, timestamp=timestamp,
            success=False, error=str(e)
        )


def start():
    uvicorn.run(app, host="0.0.0.0", port=settings.mcp_server_port, log_level="info")


if __name__ == "__main__":
    start()
