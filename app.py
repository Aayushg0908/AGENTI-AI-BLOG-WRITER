from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import date
from pathlib import Path
from typing import Any, Generator, Literal, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from langgraph.types import Command
from pydantic import BaseModel, Field

from backend import (
    CHECKPOINTER_KIND,
    LLM_DESCRIPTION,
    MAX_REVIEW_ROUNDS,
    OUTPUTS_DIR,
    app as workflow,
    validate_plan,
)

# ---------------------------------------------------------
# Paths / logging
# ---------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("langgraph-fastapi")

app = FastAPI(
    title="AgentWriter AI",
    description="FastAPI frontend for the AgentWriter LangGraph workflow.",
    version="2.0.0",
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

STAGE_LABELS = {
    "router": "Analyze the request",
    "research": "Research sources",
    "orchestrator": "Create the article plan",
    "human_review": "Review the outline",
    "workers": "Write the sections",
    "editor": "Editorial review",
    "assemble": "Assemble & verify citations",
    "diagrams": "Draw Mermaid diagrams",
    "seo": "SEO & publishing pack",
    "finalize": "Save the article",
}


# ---------------------------------------------------------
# Request schemas
# ---------------------------------------------------------
class AgentRunRequest(BaseModel):
    topic: str = Field(..., min_length=3, max_length=1000)
    audience: str = Field("", max_length=200)
    tone: str = Field("", max_length=100)
    length: Literal["short", "medium", "long"] = "medium"
    require_approval: bool = True


class ResumeRequest(BaseModel):
    action: Literal["approve", "regenerate"] = "approve"
    plan: Optional[dict] = None
    feedback: str = Field("", max_length=2000)


# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------
def make_serializable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return make_serializable(value.model_dump())
    if isinstance(value, dict):
        return {str(k): make_serializable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [make_serializable(v) for v in value]
    return value


def sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(jsonable_encoder(payload), ensure_ascii=False)}\n\n"


def stage(stage_id: str, status: str, detail: str = "") -> str:
    return sse(
        {
            "type": "stage",
            "id": stage_id,
            "label": STAGE_LABELS.get(stage_id, stage_id),
            "status": status,
            "detail": detail,
        }
    )


def task_map_from(plan: Any) -> dict[int, dict]:
    tasks = (plan or {}).get("tasks", []) if isinstance(plan, dict) else []
    result = {}
    for task in tasks:
        try:
            result[int(task["id"])] = task
        except (KeyError, TypeError, ValueError):
            continue
    return result


def check_run_id(run_id: str) -> str:
    if not RUN_ID_RE.match(run_id):
        raise HTTPException(status_code=400, detail="Invalid run ID.")
    return run_id


def run_config(run_id: str) -> dict:
    return {"configurable": {"thread_id": run_id}}


def pending_review(run_id: str) -> Optional[dict]:
    """Return the interrupt payload if this run is waiting for outline approval."""
    snapshot = workflow.get_state(run_config(run_id))
    for task in getattr(snapshot, "tasks", ()) or ():
        for item in getattr(task, "interrupts", ()) or ():
            value = getattr(item, "value", None)
            if isinstance(value, dict) and value.get("type") == "plan_review":
                return value
    return None


# ---------------------------------------------------------
# Streaming: translate LangGraph node updates into UI events
# ---------------------------------------------------------
class RunContext:
    def __init__(self, run_id: str, require_approval: bool, plan: Optional[dict] = None):
        self.run_id = run_id
        self.require_approval = require_approval
        self.task_map = task_map_from(plan)
        self.completed: set[int] = set()
        self.revising: set[int] = set()
        self.editor_done = False
        self.final = ""
        self.paused = False


def handle_update(node: str, update: dict, ctx: RunContext) -> Generator[str, None, None]:
    if node == "router":
        mode = str(update.get("mode", "closed_book"))
        needs_research = bool(update.get("needs_research"))
        recency = update.get("recency_days")
        yield sse(
            {
                "type": "routing",
                "mode": mode,
                "needs_research": needs_research,
                "queries": update.get("queries", []),
                "recency_days": recency,
            }
        )
        window = f" Sources limited to the last {recency} days." if needs_research and recency else ""
        yield stage("router", "completed", f"Selected {mode.replace('_', ' ')} mode.{window}")
        if needs_research and update.get("queries"):
            yield stage("research", "running", "Searching the web in parallel and filtering by recency.")
        else:
            yield stage("orchestrator", "running", "Creating sections, goals and word targets.")

    elif node == "research":
        evidence = update.get("evidence") or []
        yield sse({"type": "research_complete", "count": len(evidence), "evidence": evidence[:12]})
        yield stage("research", "completed", f"Kept {len(evidence)} verified sources.")
        yield stage("orchestrator", "running", "Creating sections, goals and word targets.")

    elif node == "orchestrator":
        plan = update.get("plan") or {}
        ctx.task_map = task_map_from(plan)
        yield sse({"type": "plan", "plan": plan, "editable": False})
        yield stage("orchestrator", "completed", f"Created {len(ctx.task_map)} sections.")
        if ctx.require_approval:
            yield stage("human_review", "running", "Waiting for your approval of the outline.")
        else:
            yield stage("workers", "running", "Section writers are working in parallel.")

    elif node == "__interrupt__":
        for item in update.get("__interrupt__", []):
            value = item.get("value") if isinstance(item, dict) else None
            if isinstance(value, dict) and value.get("type") == "plan_review":
                ctx.paused = True
                ctx.task_map = task_map_from(value.get("plan"))
                yield sse({"type": "plan_review", "run_id": ctx.run_id, "plan": value.get("plan")})
                yield stage("human_review", "waiting", "Edit the outline, then approve it or ask for a new one.")

    elif node == "human_review":
        if update.get("plan_approved"):
            plan = update.get("plan")
            if plan:
                ctx.task_map = task_map_from(plan)
                yield sse({"type": "plan", "plan": plan, "editable": False})
            if ctx.require_approval:
                yield stage("human_review", "completed", "Outline approved.")
            yield stage("workers", "running", "Section writers are working in parallel.")
        else:
            yield stage("human_review", "completed", "Feedback sent; regenerating the outline.")
            yield stage("orchestrator", "running", "Rewriting the outline using your feedback.")

    elif node == "worker":
        for raw_id, markdown in (update.get("sections") or {}).items():
            try:
                task_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if task_id in ctx.completed:
                continue
            ctx.completed.add(task_id)
            yield sse(
                {
                    "type": "section_complete",
                    "task_id": task_id,
                    "title": ctx.task_map.get(task_id, {}).get("title", f"Section {task_id}"),
                    "markdown": str(markdown),
                    "completed": len(ctx.completed),
                    "total": len(ctx.task_map),
                }
            )
        if ctx.task_map and len(ctx.completed) >= len(ctx.task_map):
            yield stage("workers", "completed", f"Wrote all {len(ctx.task_map)} sections.")
            yield stage("editor", "running", "The editor is reviewing every section.")

    elif node == "editor_review":
        review = update.get("review") or {}
        flagged = [s for s in review.get("sections", []) if s.get("verdict") == "revise"]
        will_revise = bool(flagged) and int(review.get("round", 1)) <= MAX_REVIEW_ROUNDS
        yield sse({"type": "review", "review": review, "will_revise": will_revise})
        if will_revise:
            ctx.revising = {int(s["task_id"]) for s in flagged}
            yield stage("editor", "running", f"Round {review.get('round', 1)}: revising {len(flagged)} section(s).")
        else:
            ctx.editor_done = True
            detail = (
                "All sections approved."
                if not flagged
                else f"{len(flagged)} section(s) still have notes; revision budget used."
            )
            yield stage("editor", "completed", detail)
            yield stage("assemble", "running", "Merging sections and checking every link.")

    elif node == "reviser":
        for raw_id in (update.get("sections") or {}):
            task_id = int(raw_id)
            yield sse(
                {
                    "type": "section_revised",
                    "task_id": task_id,
                    "title": ctx.task_map.get(task_id, {}).get("title", f"Section {task_id}"),
                }
            )

    elif node == "merge":
        if not ctx.editor_done:
            ctx.editor_done = True
            yield stage("editor", "completed", f"Revised {len(ctx.revising)} section(s).")
            yield stage("assemble", "running", "Merging sections and checking every link.")

    elif node == "citation_check":
        report = update.get("citation_report") or {}
        yield sse({"type": "citations", "report": report})
        detail = f"{report.get('verified', 0)} verified link(s)"
        if report.get("removed"):
            detail += f", removed {report['removed']} unverifiable link(s)"
        yield stage("assemble", "completed", detail + ".")
        yield stage("diagrams", "running", "Deciding where a diagram helps and drawing it in Mermaid.")

    elif node == "diagrams":
        diagrams = update.get("diagrams") or []
        yield sse({"type": "diagrams", "count": len(diagrams), "diagrams": diagrams})
        yield stage(
            "diagrams",
            "completed",
            f"Added {len(diagrams)} diagram{'' if len(diagrams) == 1 else 's'}."
            if diagrams else "No diagram needed.",
        )
        yield stage("seo", "running", "Writing the title, meta description, slug and tags.")

    elif node == "seo":
        yield sse({"type": "seo", "seo": update.get("seo") or {}})
        yield stage("seo", "completed", "Publishing pack ready.")
        yield stage("finalize", "running", "Saving the article.")

    elif node == "finalize":
        ctx.final = str(update.get("final") or "")
        yield stage("finalize", "completed", "The final Markdown article is ready.")


def stream_run(graph_input: Any, ctx: RunContext) -> Generator[str, None, None]:
    config = run_config(ctx.run_id)
    try:
        for chunk in workflow.stream(graph_input, config=config, stream_mode="updates"):
            if not isinstance(chunk, dict):
                continue
            for node, raw_update in chunk.items():
                if node == "__interrupt__":
                    items = raw_update if isinstance(raw_update, (list, tuple)) else [raw_update]
                    update = {
                        "__interrupt__": [
                            {"value": make_serializable(getattr(item, "value", None))} for item in items
                        ]
                    }
                    yield from handle_update(node, update, ctx)
                    continue
                update = make_serializable(raw_update)
                if not isinstance(update, dict):
                    update = {}
                yield from handle_update(node, update, ctx)

        if ctx.paused:
            yield sse({"type": "paused", "run_id": ctx.run_id})
            return

        if not ctx.final:
            values = getattr(workflow.get_state(config), "values", {}) or {}
            ctx.final = str(values.get("final") or "")
        if not ctx.final:
            raise RuntimeError("The workflow completed but did not return final Markdown.")

        yield sse(
            {
                "type": "final",
                "run_id": ctx.run_id,
                "markdown": ctx.final,
                "download_url": f"/api/runs/{ctx.run_id}/download",
            }
        )
        yield sse({"type": "done", "run_id": ctx.run_id})

    except GeneratorExit:
        logger.info("Browser disconnected from run %s", ctx.run_id)
        raise
    except Exception as error:  # noqa: BLE001
        logger.exception("Workflow run %s failed", ctx.run_id)
        yield sse({"type": "error", "run_id": ctx.run_id, "message": str(error)})


def streaming_response(generator: Generator[str, None, None]) -> StreamingResponse:
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------
# Routes
# ---------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"page_title": "AgentWriter AI"},
    )


@app.get("/api/health")
def health_check():
    return {
        "status": "ok",
        "workflow": "loaded",
        "llm": LLM_DESCRIPTION,
        "checkpointer": CHECKPOINTER_KIND,
    }


@app.post("/api/run")
def run_agent(request_data: AgentRunRequest):
    topic = request_data.topic.strip()
    if len(topic) < 3:
        raise HTTPException(status_code=422, detail="Please provide a valid topic.")

    run_id = uuid.uuid4().hex
    graph_input = {
        "topic": topic,
        "audience": request_data.audience.strip(),
        "tone": request_data.tone.strip(),
        "length": request_data.length,
        "require_approval": request_data.require_approval,
        "as_of": date.today().isoformat(),
        "sections": {},
        "review_round": 0,
    }
    ctx = RunContext(run_id, request_data.require_approval)

    def generator():
        yield sse({"type": "run_started", "run_id": run_id, "topic": topic})
        yield stage("router", "running", "Deciding whether the topic needs current web research.")
        yield from stream_run(graph_input, ctx)

    return streaming_response(generator())


@app.post("/api/runs/{run_id}/resume")
def resume_run(run_id: str, request_data: ResumeRequest):
    check_run_id(run_id)
    review = pending_review(run_id)
    if review is None:
        raise HTTPException(status_code=409, detail="This run is not waiting for outline approval.")

    payload: dict[str, Any] = {"action": request_data.action}
    if request_data.action == "regenerate":
        payload["feedback"] = request_data.feedback.strip()
    elif request_data.plan is not None:
        values = getattr(workflow.get_state(run_config(run_id)), "values", {}) or {}
        try:
            payload["plan"] = validate_plan(request_data.plan, values)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    plan = payload.get("plan") or review.get("plan")
    ctx = RunContext(run_id, require_approval=True, plan=plan)

    def generator():
        yield sse({"type": "run_resumed", "run_id": run_id, "action": request_data.action})
        yield from stream_run(Command(resume=payload), ctx)

    return streaming_response(generator())


@app.get("/api/runs/{run_id}/download")
def download_markdown(run_id: str):
    check_run_id(run_id)
    run_dir = OUTPUTS_DIR / run_id
    output_file = run_dir / "blog.md"
    if not output_file.is_file():
        raise HTTPException(status_code=404, detail="Generated Markdown file was not found.")

    slug = f"generated-blog-{run_id[:8]}"
    try:
        meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        slug = meta.get("seo", {}).get("slug") or slug
    except (OSError, ValueError):
        pass

    return FileResponse(path=output_file, media_type="text/markdown", filename=f"{slug}.md")


if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
