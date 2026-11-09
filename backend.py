"""
AgentWriter-AI LangGraph workflow.

router -> (research) -> orchestrator -> human_review (interrupt)
       -> worker x N -> editor_review <-> reviser
       -> merge -> citation_check -> diagrams (Mermaid) -> seo -> finalize
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Callable, Dict, List, Literal, Optional, TypedDict
from urllib.parse import urlparse

from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

load_dotenv()

logger = logging.getLogger("agentwriter")

BASE_DIR = Path(__file__).resolve().parent
OUTPUTS_DIR = BASE_DIR / "outputs"

LLM_RETRIES = max(1, int(os.getenv("LLM_RETRIES", "3")))
MAX_REVIEW_ROUNDS = max(0, int(os.getenv("MAX_REVIEW_ROUNDS", "1")))
MAX_QUERIES = 8
MAX_EVIDENCE = 16
MAX_DIAGRAMS = 3

LENGTH_GUIDE = {
    "short": "4–5 sections, roughly 900–1,200 words in total",
    "medium": "5–7 sections, roughly 1,500–2,200 words in total",
    "long": "7–9 sections, roughly 2,500–3,500 words in total",
}


# =============================================================
# 1) LLM (provider is configurable through environment variables)
# =============================================================
def build_llm():
    provider = os.getenv("LLM_PROVIDER", "groq").strip().lower()
    model = os.getenv("LLM_MODEL", "").strip()
    temperature = float(os.getenv("LLM_TEMPERATURE", "0"))

    if provider == "groq":
        from langchain_groq import ChatGroq

        if not os.getenv("GROQ_API_KEY"):
            raise RuntimeError("GROQ_API_KEY is missing. Add it to .env (or set LLM_PROVIDER).")

        return provider, ChatGroq(
            model=model or "openai/gpt-oss-120b",
            temperature=temperature,
        )

    if provider in {"google", "gemini"}:
        from langchain_google_genai import ChatGoogleGenerativeAI

        return provider, ChatGoogleGenerativeAI(
            model=model or "gemini-2.5-flash",
            temperature=temperature,
        )

    if provider == "ollama":
        from langchain_ollama import ChatOllama

        return provider, ChatOllama(
            model=model or "mistral",
            base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            temperature=temperature,
            # Ollama's default context window is small and silently
            # truncates long prompts (evidence packs, full drafts).
            num_ctx=int(os.getenv("OLLAMA_NUM_CTX", "16384")),
        )

    raise ValueError(
        f"Unsupported LLM_PROVIDER '{provider}'. Use ollama, groq or google."
    )


LLM_PROVIDER, llm = build_llm()
LLM_DESCRIPTION = f"{LLM_PROVIDER}:{getattr(llm, 'model', None) or getattr(llm, 'model_name', '')}"


# -------------------------------------------------------------
# Per-run user keys ("bring your own key").
# Held in process memory only, keyed by run id. Never written to graph state,
# checkpoints or output files. Empty -> the server's own keys are used.
# -------------------------------------------------------------
_RUN_KEYS: Dict[str, Dict[str, str]] = {}


def set_run_keys(run_id: str, keys: Optional[Dict[str, str]]) -> None:
    clean = {k: v.strip() for k, v in (keys or {}).items() if isinstance(v, str) and v.strip()}
    if clean:
        _RUN_KEYS[run_id] = clean
    else:
        _RUN_KEYS.pop(run_id, None)


def clear_run_keys(run_id: str) -> None:
    _RUN_KEYS.pop(run_id, None)


def _run_keys(config: Optional[RunnableConfig]) -> Dict[str, str]:
    thread_id = ((config or {}).get("configurable") or {}).get("thread_id")
    return _RUN_KEYS.get(str(thread_id), {}) if thread_id else {}


@lru_cache(maxsize=32)
def _user_groq_llm(api_key: str):
    from langchain_groq import ChatGroq

    model = os.getenv("LLM_MODEL", "").strip() if LLM_PROVIDER == "groq" else ""
    return ChatGroq(
        model=model or "openai/gpt-oss-120b",
        temperature=float(os.getenv("LLM_TEMPERATURE", "0")),
        api_key=api_key,
    )


def llm_for(config: Optional[RunnableConfig] = None):
    """The user's own Groq key if they supplied one for this run, else the server LLM."""
    key = _run_keys(config).get("groq_api_key")
    return _user_groq_llm(key) if key else llm


RATE_LIMIT_RETRIES = max(0, int(os.getenv("RATE_LIMIT_RETRIES", "8")))
MAX_RATE_LIMIT_WAIT = 90.0  # seconds; longer waits (e.g. daily quota) fail fast instead
_RETRY_AFTER_RE = re.compile(r"try again in (?:(\d+)h)?(?:(\d+)m)?([\d.]+)s", re.I)


def _rate_limit_wait(error: Exception, attempt: int) -> Optional[float]:
    """Seconds to wait before retrying a rate-limited call, or None if not a rate limit."""
    text = str(error)
    if getattr(error, "status_code", None) != 429 and "429" not in text and "rate limit" not in text.lower():
        return None
    match = _RETRY_AFTER_RE.search(text)
    if match:
        hours, minutes, seconds = match.groups()
        wait = int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(seconds)
    else:
        wait = 2.0 ** attempt
    return wait + random.uniform(0.5, 2.0)


def _with_retries(label: str, call: Callable[[], Any]) -> Any:
    """Run an LLM call with retries.

    - Rate limits (HTTP 429): wait as long as the provider asks, up to RATE_LIMIT_RETRIES times.
    - Other errors / empty output: up to LLM_RETRIES attempts.
    """
    failures = 0
    rate_limited = 0
    last_error: Exception | None = None

    while True:
        try:
            result = call()
            if result:
                return result
            last_error = ValueError("model returned empty output")
        except Exception as error:  # noqa: BLE001 - provider errors vary
            wait = _rate_limit_wait(error, rate_limited + 1)
            if wait is not None and rate_limited < RATE_LIMIT_RETRIES and wait <= MAX_RATE_LIMIT_WAIT:
                rate_limited += 1
                logger.warning("%s rate-limited; waiting %.1fs (%d/%d)", label, wait, rate_limited, RATE_LIMIT_RETRIES)
                time.sleep(wait)
                continue
            last_error = error

        failures += 1
        logger.warning("%s attempt %d/%d failed: %s", label, failures, LLM_RETRIES, last_error)
        if failures >= LLM_RETRIES:
            raise RuntimeError(f"{label} generation failed after {failures} attempts: {last_error}")


def invoke_structured(config: Optional[RunnableConfig], schema: type[BaseModel], messages: list) -> Any:
    """Structured-output call with retries (also retries empty results)."""
    runnable = llm_for(config).with_structured_output(schema)
    return _with_retries(schema.__name__, lambda: runnable.invoke(messages))


def invoke_text(config: Optional[RunnableConfig], messages: list) -> str:
    def call() -> str:
        content = llm_for(config).invoke(messages).content
        if isinstance(content, list):  # some providers return content blocks
            content = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )
        return str(content).strip()

    return _with_retries("Text", call)


# =============================================================
# 2) Schemas
# =============================================================
class Task(BaseModel):
    id: int
    title: str
    goal: str = Field(
        ...,
        description="One sentence describing what the reader should be able to do/understand after this section.",
    )
    bullets: List[str] = Field(
        ...,
        min_length=1,
        max_length=8,
        description="3–6 concrete, non-overlapping subpoints to cover in this section.",
    )
    target_words: int = Field(..., description="Target word count for this section (120–550).")
    tags: List[str] = Field(default_factory=list)
    requires_research: bool = False
    requires_citations: bool = False
    requires_code: bool = False


class Plan(BaseModel):
    blog_title: str
    audience: str
    tone: str
    blog_kind: Literal["explainer", "tutorial", "news_roundup", "comparison", "system_design"] = "explainer"
    constraints: List[str] = Field(default_factory=list)
    tasks: List[Task] = Field(..., min_length=1)


class EvidenceItem(BaseModel):
    title: str
    url: str
    published_at: Optional[str] = None
    snippet: Optional[str] = None
    source: Optional[str] = None


class EvidencePack(BaseModel):
    evidence: List[EvidenceItem] = Field(default_factory=list)


class RouterDecision(BaseModel):
    needs_research: bool
    mode: Literal["closed_book", "hybrid", "open_book"]
    queries: List[str] = Field(default_factory=list)
    recency_days: Optional[int] = Field(
        None,
        description="Only keep sources published within this many days (null = no limit).",
    )


class SectionReview(BaseModel):
    task_id: int
    verdict: Literal["ok", "revise"]
    issues: List[str] = Field(default_factory=list)


class ReviewReport(BaseModel):
    overall: str = Field(..., description="1–3 sentence assessment of the whole draft.")
    sections: List[SectionReview] = Field(default_factory=list)


class DiagramSpec(BaseModel):
    section_title: str = Field(..., description="Exact title of the section the diagram belongs to.")
    title: str
    caption: str
    mermaid: str = Field(..., description="Mermaid source code, without ``` fences.")


class DiagramPlan(BaseModel):
    diagrams: List[DiagramSpec] = Field(default_factory=list)


class SeoPack(BaseModel):
    seo_title: str = Field(..., description="Search-friendly title, max 60 characters.")
    meta_description: str = Field(..., description="Meta description, 140–160 characters.")
    slug: str = Field(..., description="URL slug: lowercase words joined by hyphens.")
    keywords: List[str] = Field(default_factory=list, description="5–10 search keywords.")
    tags: List[str] = Field(default_factory=list, description="3–5 platform tags (dev.to style).")
    summary: str = Field(..., description="2–3 sentence summary / TL;DR.")
    social_post: str = Field(..., description="Short LinkedIn/X post announcing the article, max 280 characters.")


# =============================================================
# 3) State
# =============================================================
def merge_sections(left: Optional[Dict[str, str]], right: Optional[Dict[str, str]]) -> Dict[str, str]:
    """Sections keyed by task id; later writes (revisions) replace earlier ones."""
    merged = dict(left or {})
    merged.update(right or {})
    return merged


class State(TypedDict, total=False):
    # user inputs
    topic: str
    audience: str
    tone: str
    length: str
    require_approval: bool
    as_of: str

    # routing / research
    mode: str
    needs_research: bool
    queries: List[str]
    recency_days: Optional[int]
    evidence: List[dict]

    # planning + human review
    plan: Optional[dict]
    plan_feedback: str
    plan_approved: bool

    # writing + editing
    sections: Annotated[Dict[str, str], merge_sections]
    review: dict
    review_round: int

    # assembly
    merged_md: str
    citation_report: dict
    diagrams: List[dict]
    md_with_diagrams: str
    seo: dict
    final: str
    output_path: str


# =============================================================
# Helpers
# =============================================================
def _today(state: State) -> date:
    try:
        return date.fromisoformat(state.get("as_of") or "")
    except ValueError:
        return date.today()


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(text).date()
    except (TypeError, ValueError, IndexError):
        return None


def _normalize_url(url: str) -> str:
    return (url or "").strip().split("#")[0].rstrip("/").lower()


def _slugify(text: str, max_length: int = 80) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    slug = slug[:max_length].rstrip("-")
    return slug or "article"


def _word_count(markdown: str) -> int:
    without_code = re.sub(r"```.*?```", " ", markdown or "", flags=re.S)
    return len(re.findall(r"\b\w+\b", without_code))


def _plan(state: State) -> Plan:
    return Plan(**state["plan"])


def _evidence_lines(evidence: List[dict], limit: int = 20) -> str:
    return "\n".join(
        f"- {e.get('title', '')} | {e.get('url', '')} | {e.get('published_at') or 'date:unknown'}"
        for e in evidence[:limit]
    )


def normalize_plan(plan: Plan, state: State | dict) -> Plan:
    """Renumber tasks, clamp word targets and honour user-specified audience/tone."""
    tasks = []
    for index, task in enumerate(plan.tasks, start=1):
        bullets = [b.strip() for b in task.bullets if b and b.strip()] or [task.goal]
        tasks.append(
            task.model_copy(
                update={
                    "id": index,
                    "title": task.title.strip() or f"Section {index}",
                    "bullets": bullets[:8],
                    "target_words": min(800, max(80, int(task.target_words or 300))),
                }
            )
        )

    update: dict[str, Any] = {"tasks": tasks}
    if (state.get("audience") or "").strip():
        update["audience"] = state["audience"].strip()
    if (state.get("tone") or "").strip():
        update["tone"] = state["tone"].strip()
    return plan.model_copy(update=update)


def validate_plan(payload: dict, state: dict | None = None) -> dict:
    """Validate a user-edited plan coming from the UI. Raises ValueError."""
    try:
        plan = Plan(**payload)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in error.errors()[:5]
        )
        raise ValueError(f"Invalid plan: {problems}") from error
    return normalize_plan(plan, state or {}).model_dump()


# =============================================================
# 4) Router
# =============================================================
ROUTER_SYSTEM = """You are a routing module for a technical blog planner.

Decide whether web research is needed BEFORE planning.

Modes:
- closed_book (needs_research=false):
  Evergreen topics where correctness does not depend on recent facts (concepts, fundamentals).
- hybrid (needs_research=true):
  Mostly evergreen but needs up-to-date examples/tools/models to be useful.
- open_book (needs_research=true):
  Mostly volatile: weekly roundups, "this week", "latest", rankings, pricing, policy/regulation.

If needs_research=true:
- Output 3–8 high-signal queries.
- Queries should be scoped and specific (avoid generic queries like just "AI" or "LLM").
- If the user asked for "last week/this week/latest", reflect that constraint IN THE QUERIES,
  using the real dates relative to today's date.

recency_days (how fresh sources must be):
- open_book: required. "today" -> 1, "this week"/"last week" -> 7, "latest"/"this month" -> 30,
  "this year" -> 365.
- hybrid: usually 365 (recent tools/models), or null if age does not matter.
- closed_book: null.
"""


def router_node(state: State, config: RunnableConfig) -> dict:
    as_of = _today(state).isoformat()
    decision: RouterDecision = invoke_structured(
        config,
        RouterDecision,
        [
            SystemMessage(content=ROUTER_SYSTEM),
            HumanMessage(content=f"Today's date: {as_of}\nTopic: {state['topic']}"),
        ],
    )

    needs_research = decision.needs_research and decision.mode != "closed_book"
    recency = decision.recency_days if needs_research else None
    if recency is not None and recency <= 0:
        recency = None
    if decision.mode == "open_book" and recency is None:
        recency = 30

    return {
        "as_of": as_of,
        "needs_research": needs_research,
        "mode": decision.mode if needs_research else "closed_book",
        "queries": [q.strip() for q in decision.queries if q and q.strip()][:MAX_QUERIES],
        "recency_days": recency,
    }


def route_next(state: State) -> str:
    return "research" if state.get("needs_research") and state.get("queries") else "orchestrator"


# =============================================================
# 5) Research (Tavily)
# =============================================================
def _time_range_for(days: Optional[int]) -> Optional[str]:
    if not days:
        return None
    if days <= 1:
        return "day"
    if days <= 7:
        return "week"
    if days <= 31:
        return "month"
    if days <= 366:
        return "year"
    return None


def _tavily_search(
    query: str,
    *,
    max_results: int,
    topic: str,
    time_range: Optional[str],
    api_key: Optional[str] = None,
) -> tuple[List[dict], Optional[str]]:
    """Return (results, error). One failed query should not kill research."""
    from langchain_tavily import TavilySearch

    try:
        extra = {"tavily_api_key": api_key} if api_key else {}
        tool = TavilySearch(max_results=max_results, topic=topic, time_range=time_range, **extra)
        response = tool.invoke({"query": query})
    except Exception as error:  # noqa: BLE001
        logger.warning("Tavily query failed (%s): %s", query, error)
        return [], str(error)

    if not isinstance(response, dict):
        return [], "unexpected Tavily response"
    if response.get("error"):
        return [], str(response["error"])

    results = []
    for item in response.get("results") or []:
        url = (item.get("url") or "").strip()
        if not url:
            continue
        results.append(
            {
                "title": (item.get("title") or "").strip(),
                "url": url,
                "snippet": (item.get("content") or "")[:600],
                "published_at": item.get("published_date") or item.get("published_at"),
                "source": urlparse(url).netloc,
            }
        )
    return results, None


RESEARCH_SYSTEM = """You are a research synthesizer for technical writing.

Given raw web search results, select the useful ones as EvidenceItem objects.

Rules:
- Copy each url EXACTLY as it appears in the input. Never invent or modify URLs.
- Prefer relevant + authoritative sources (official docs, company/engineering blogs, reputable outlets).
- Drop results that are off-topic, spammy, or duplicate another result.
- Keep snippets short (1–2 sentences) and factual.
- Leave published_at null; dates are filled in from the raw data afterwards.
"""


def research_node(state: State, config: RunnableConfig) -> dict:
    queries = (state.get("queries") or [])[:MAX_QUERIES]
    mode = state.get("mode", "hybrid")
    recency_days = state.get("recency_days")
    search_topic = "news" if mode == "open_book" else "general"
    time_range = _time_range_for(recency_days)

    if not queries:
        return {"evidence": []}

    user_key = _run_keys(config).get("tavily_api_key")
    with ThreadPoolExecutor(max_workers=min(4, len(queries))) as pool:
        outcomes = list(
            pool.map(
                lambda q: _tavily_search(
                    q, max_results=6, topic=search_topic, time_range=time_range, api_key=user_key
                ),
                queries,
            )
        )
    batches = [results for results, _ in outcomes]
    errors = [error for _, error in outcomes if error]
    if user_key and errors and len(errors) == len(outcomes):
        # Tell the user their own key is the problem instead of silently skipping research.
        raise RuntimeError(f"Web search failed with your Tavily key: {errors[0][:200]}")

    # Deduplicate by URL and drop results older than the recency window.
    cutoff = _today(state) - timedelta(days=recency_days) if recency_days else None
    raw: Dict[str, dict] = {}
    for batch in batches:
        for item in batch:
            key = _normalize_url(item["url"])
            if key in raw:
                continue
            published = _parse_date(item.get("published_at"))
            item["published_at"] = published.isoformat() if published else None
            if cutoff and published and published < cutoff:
                continue
            raw[key] = item

    if not raw:
        return {"evidence": []}

    raw_list = list(raw.values())
    compact = "\n".join(
        f"- title: {r['title']}\n  url: {r['url']}\n  snippet: {r['snippet'][:400]}"
        for r in raw_list[:40]
    )

    evidence: List[dict] = []
    try:
        pack: EvidencePack = invoke_structured(
            config,
            EvidencePack,
            [
                SystemMessage(content=RESEARCH_SYSTEM),
                HumanMessage(content=f"Topic: {state['topic']}\n\nRaw results:\n{compact}"),
            ],
        )
        seen: set[str] = set()
        for item in pack.evidence:
            key = _normalize_url(item.url)
            source = raw.get(key)
            # Only keep URLs that actually came back from search (no hallucinated links).
            if not source or key in seen:
                continue
            seen.add(key)
            evidence.append(
                {
                    "title": item.title or source["title"],
                    "url": source["url"],
                    "published_at": source["published_at"],
                    "snippet": (item.snippet or source["snippet"])[:400],
                    "source": source["source"],
                }
            )
    except RuntimeError as error:
        logger.warning("Evidence synthesis failed, using raw results: %s", error)

    if not evidence:
        evidence = [{**r, "snippet": r["snippet"][:400]} for r in raw_list]

    return {"evidence": evidence[:MAX_EVIDENCE]}


# =============================================================
# 6) Orchestrator (plan)
# =============================================================
ORCH_SYSTEM = """You are a senior technical writer and developer advocate.
Your job is to produce a highly actionable outline for a technical blog post.

Hard requirements:
- Follow the length guide for the number of sections and total words.
- Each task must include:
  1) goal (1 sentence)
  2) 3–6 bullets that are concrete, specific, and non-overlapping
  3) target word count (120–550); the sum should match the length guide
- Use the requested audience and tone when provided.

Quality bar:
- Use correct terminology for the audience.
- Bullets must be actionable: build/compare/measure/verify/debug.
- Ensure the overall plan includes at least 2 of these somewhere:
  * minimal code sketch / MWE (set requires_code=True for that section)
  * edge cases / failure modes
  * performance/cost considerations
  * security/privacy considerations (if relevant)
  * debugging/observability tips
- No two sections may cover the same ground.

Grounding rules:
- Mode closed_book: keep it evergreen; do not depend on evidence.
- Mode hybrid:
  - Use evidence for up-to-date examples (models/tools/releases) in bullets.
  - Mark sections using fresh info as requires_research=True and requires_citations=True.
- Mode open_book:
  - Set blog_kind = "news_roundup".
  - Every section is about summarizing events + implications.
  - DO NOT include tutorial/how-to sections unless the user explicitly asked for that.
  - If evidence is empty or insufficient, create a plan that transparently says "insufficient sources"
    and includes only what can be supported.

If reviewer feedback on a previous outline is provided, address ALL of it.

Output must strictly match the Plan schema.
"""


def orchestrator_node(state: State, config: RunnableConfig) -> dict:
    mode = state.get("mode", "closed_book")
    length = state.get("length") or "medium"
    feedback = (state.get("plan_feedback") or "").strip()

    parts = [
        f"Today's date: {_today(state).isoformat()}",
        f"Topic: {state['topic']}",
        f"Mode: {mode}",
        f"Audience: {state.get('audience') or 'choose the most suitable developer audience'}",
        f"Tone: {state.get('tone') or 'choose a suitable tone (precise, practical)'}",
        f"Length guide: {LENGTH_GUIDE.get(length, LENGTH_GUIDE['medium'])}",
        "",
        "Evidence (ONLY use for fresh claims; may be empty):",
        _evidence_lines(state.get("evidence") or [], limit=MAX_EVIDENCE) or "(none)",
    ]
    if feedback and state.get("plan"):
        parts += [
            "",
            "Previous outline:",
            json.dumps(state["plan"], ensure_ascii=False)[:6000],
            "",
            f"Reviewer feedback on the previous outline: {feedback}",
        ]

    plan: Plan = invoke_structured(
        config,
        Plan,
        [SystemMessage(content=ORCH_SYSTEM), HumanMessage(content="\n".join(parts))],
    )
    if mode == "open_book":
        plan = plan.model_copy(update={"blog_kind": "news_roundup"})

    return {
        "plan": normalize_plan(plan, state).model_dump(),
        "plan_feedback": "",
        "plan_approved": False,
    }


# =============================================================
# 7) Human review of the outline (LangGraph interrupt)
# =============================================================
def human_review_node(state: State) -> dict:
    if not state.get("require_approval"):
        return {"plan_approved": True}

    decision = interrupt({"type": "plan_review", "plan": state["plan"]})
    if not isinstance(decision, dict):
        decision = {}

    if decision.get("action") == "regenerate":
        feedback = str(decision.get("feedback") or "").strip() or "Improve the outline."
        return {"plan_approved": False, "plan_feedback": feedback[:2000]}

    plan = state["plan"]
    if decision.get("plan"):
        plan = validate_plan(decision["plan"], state)
    return {"plan": plan, "plan_approved": True}


def _worker_payload(state: State, task: dict) -> dict:
    plan = state["plan"]
    return {
        "task": task,
        "topic": state["topic"],
        "mode": state.get("mode", "closed_book"),
        "as_of": _today(state).isoformat(),
        "plan": plan,
        "outline": [f"{t['id']}. {t['title']}" for t in plan["tasks"]],
        "evidence": state.get("evidence") or [],
    }


def after_review(state: State):
    if not state.get("plan_approved"):
        return "orchestrator"
    return [Send("worker", _worker_payload(state, task)) for task in state["plan"]["tasks"]]


# =============================================================
# 8) Worker / reviser (write one section)
# =============================================================
WORKER_SYSTEM = """You are a senior technical writer and developer advocate.
Write ONE section of a technical blog post in Markdown.

Hard constraints:
- Follow the provided Goal and cover ALL Bullets in order (do not skip or merge bullets).
- Stay close to Target words (±15%).
- Output ONLY the section content in Markdown (no blog title H1, no extra commentary).
- Start with a '## <Section Title>' heading.
- Do not repeat material that belongs to the other sections in the outline.

Scope guard:
- If blog_kind == "news_roundup": do NOT turn this into a tutorial/how-to guide.
  Do NOT teach web scraping, RSS, automation, or "how to fetch news" unless bullets explicitly ask for it.
  Focus on summarizing events and implications.

Grounding policy:
- If mode == open_book:
  - Do NOT introduce any specific event/company/model/funding/policy claim unless it is supported by provided Evidence URLs.
  - For each event claim, attach a source as a Markdown link: ([Source](URL)).
  - Only use URLs provided in Evidence. If not supported, write: "Not found in provided sources."
- If requires_citations == true:
  - For outside-world claims, cite Evidence URLs the same way.
- Never invent URLs. Links that are not in Evidence will be removed.
- Evergreen reasoning is OK without citations unless requires_citations is true.

Code:
- If requires_code == true, include at least one minimal, correct code snippet relevant to the bullets.

Style:
- Short paragraphs, bullets where helpful, code fences for code.
- Avoid fluff/marketing. Be precise and implementation-oriented.
"""


def _clean_section(markdown: str, title: str) -> str:
    text = markdown.strip()
    fenced = re.fullmatch(r"```(?:markdown|md)?\s*\n(.*)\n```", text, flags=re.S)
    if fenced:
        text = fenced.group(1).strip()
    text = re.sub(r"^# [^\n]*\n+", "", text)  # drop any stray H1
    if not text.startswith("## "):
        text = f"## {title}\n\n{text}"
    return text


def _write_section(payload: dict, config: Optional[RunnableConfig], *, issues: Optional[List[str]] = None, previous_draft: str = "") -> str:
    task = Task(**payload["task"])
    plan = Plan(**payload["plan"])
    evidence = payload.get("evidence") or []
    bullets_text = "\n- " + "\n- ".join(task.bullets)

    content = (
        f"Today's date: {payload.get('as_of')}\n"
        f"Blog title: {plan.blog_title}\n"
        f"Audience: {plan.audience}\n"
        f"Tone: {plan.tone}\n"
        f"Blog kind: {plan.blog_kind}\n"
        f"Constraints: {plan.constraints}\n"
        f"Topic: {payload['topic']}\n"
        f"Mode: {payload.get('mode', 'closed_book')}\n\n"
        f"Full outline (you write only section {task.id}):\n" + "\n".join(payload.get("outline") or []) + "\n\n"
        f"Section title: {task.title}\n"
        f"Goal: {task.goal}\n"
        f"Target words: {task.target_words}\n"
        f"Tags: {task.tags}\n"
        f"requires_research: {task.requires_research}\n"
        f"requires_citations: {task.requires_citations}\n"
        f"requires_code: {task.requires_code}\n"
        f"Bullets:{bullets_text}\n\n"
        f"Evidence (ONLY use these URLs when citing):\n{_evidence_lines(evidence) or '(none)'}\n"
    )

    if issues:
        content += (
            "\n\nYou are REVISING your previous draft of this section. The editor flagged these issues; "
            "fix every one of them while keeping what already works:\n- "
            + "\n- ".join(issues)
            + f"\n\nPrevious draft:\n{previous_draft}\n"
        )

    raw = invoke_text(config, [SystemMessage(content=WORKER_SYSTEM), HumanMessage(content=content)])
    return _clean_section(raw, task.title)


def worker_node(payload: dict, config: RunnableConfig) -> dict:
    task_id = str(payload["task"]["id"])
    return {"sections": {task_id: _write_section(payload, config)}}


def reviser_node(payload: dict, config: RunnableConfig) -> dict:
    task_id = str(payload["task"]["id"])
    revised = _write_section(
        payload,
        config,
        issues=payload.get("issues") or [],
        previous_draft=payload.get("previous_draft") or "",
    )
    return {"sections": {task_id: revised}}


# =============================================================
# 9) Editor / review agent
# =============================================================
EDITOR_SYSTEM = """You are the editor-in-chief reviewing a draft technical blog post, section by section.

For EVERY section return a verdict:
- "ok": publishable as-is (minor nitpicks do not count).
- "revise": has a material problem, e.g.
  * does not cover its planned bullets or drifts from its goal
  * repeats content that another section already covers
  * claims about recent events/products without a citation where citations are required
  * incorrect, incomplete or non-runnable code; wrong terminology
  * fluff, marketing language, or a tone that does not fit the audience
  * an abrupt start/end that breaks the flow of the article

Issues must be specific and actionable (what to change and where). Max 5 issues per section.
Return "overall" as a 1–3 sentence assessment of the whole draft.
"""


def _rule_based_issues(task: Task, markdown: str) -> List[str]:
    issues = []
    words = _word_count(markdown)
    if not markdown.strip():
        issues.append("Section is missing; write it.")
    elif words < task.target_words * 0.6:
        issues.append(f"Too short: {words} words vs target {task.target_words}. Expand the thinnest bullets.")
    elif words > task.target_words * 1.6:
        issues.append(f"Too long: {words} words vs target {task.target_words}. Tighten it.")
    if task.requires_code and "```" not in markdown:
        issues.append("Section requires a minimal code example but has none.")
    return issues


def editor_review_node(state: State, config: RunnableConfig) -> dict:
    plan = _plan(state)
    sections = state.get("sections") or {}
    review_round = (state.get("review_round") or 0) + 1

    blocks = []
    for task in plan.tasks:
        md = sections.get(str(task.id), "")
        blocks.append(
            f"<section id=\"{task.id}\" title=\"{task.title}\" target_words=\"{task.target_words}\" "
            f"actual_words=\"{_word_count(md)}\" requires_citations=\"{task.requires_citations}\" "
            f"requires_code=\"{task.requires_code}\">\n"
            f"Goal: {task.goal}\nPlanned bullets: {task.bullets}\n---\n{md}\n</section>"
        )

    try:
        report: ReviewReport = invoke_structured(
            config,
            ReviewReport,
            [
                SystemMessage(content=EDITOR_SYSTEM),
                HumanMessage(
                    content=(
                        f"Blog title: {plan.blog_title}\nAudience: {plan.audience}\nTone: {plan.tone}\n"
                        f"Blog kind: {plan.blog_kind}\nMode: {state.get('mode', 'closed_book')}\n\n"
                        + "\n\n".join(blocks)
                    )
                ),
            ],
        )
    except RuntimeError as error:
        logger.warning("Editor review failed, using rule-based checks only: %s", error)
        report = ReviewReport(overall="Automated editorial review unavailable; applied rule-based checks.")

    llm_reviews = {r.task_id: r for r in report.sections}
    results = []
    for task in plan.tasks:
        llm_review = llm_reviews.get(task.id)
        issues = list(llm_review.issues) if llm_review and llm_review.verdict == "revise" else []
        issues += _rule_based_issues(task, sections.get(str(task.id), ""))
        results.append(
            {
                "task_id": task.id,
                "title": task.title,
                "verdict": "revise" if issues else "ok",
                "issues": issues[:6],
            }
        )

    return {
        "review": {"round": review_round, "overall": report.overall, "sections": results},
        "review_round": review_round,
    }


def after_editor(state: State):
    review = state.get("review") or {}
    flagged = [s for s in review.get("sections", []) if s["verdict"] == "revise"]
    if not flagged or (state.get("review_round") or 0) > MAX_REVIEW_ROUNDS:
        return "merge"

    tasks = {t["id"]: t for t in state["plan"]["tasks"]}
    sections = state.get("sections") or {}
    sends = []
    for item in flagged:
        task = tasks.get(item["task_id"])
        if not task:
            continue
        payload = _worker_payload(state, task)
        payload["issues"] = item["issues"]
        payload["previous_draft"] = sections.get(str(task["id"]), "")
        sends.append(Send("reviser", payload))
    return sends or "merge"


def after_reviser(state: State) -> str:
    return "editor_review" if (state.get("review_round") or 0) < MAX_REVIEW_ROUNDS else "merge"


# =============================================================
# 10) Merge + citation verification
# =============================================================
def merge_node(state: State) -> dict:
    plan = _plan(state)
    sections = state.get("sections") or {}
    body = "\n\n".join(
        sections[str(task.id)].strip() for task in plan.tasks if sections.get(str(task.id))
    )
    return {"merged_md": f"# {plan.blog_title}\n\n{body}\n"}


SOURCE_LINK_RE = re.compile(r"\s?\(\s*\[(?:source|sources|ref|\d+)\]\((https?://[^)\s]+)\)\s*\)", re.I)
LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\((https?://[^)\s]+)\)")


def citation_check_node(state: State) -> dict:
    """Remove links that do not point at a researched source."""
    allowed = {_normalize_url(e.get("url", "")) for e in state.get("evidence") or []}
    verified: set[str] = set()
    removed: List[str] = []

    def check_source(match: re.Match) -> str:
        url = match.group(1)
        if _normalize_url(url) in allowed:
            verified.add(url)
            return match.group(0)
        removed.append(url)
        return ""

    def check_link(match: re.Match) -> str:
        text, url = match.group(1), match.group(2)
        if _normalize_url(url) in allowed:
            verified.add(url)
            return match.group(0)
        removed.append(url)
        return text

    # Leave code blocks untouched (odd segments are inside ``` fences).
    segments = re.split(r"(```.*?```)", state["merged_md"], flags=re.S)
    for index in range(0, len(segments), 2):
        segment = SOURCE_LINK_RE.sub(check_source, segments[index])
        segments[index] = LINK_RE.sub(check_link, segment)

    return {
        "merged_md": "".join(segments),
        "citation_report": {
            "verified": len(verified),
            "removed": len(removed),
            "removed_urls": removed[:20],
        },
    }


# =============================================================
# 11) Diagram agent (Mermaid)
# =============================================================
MERMAID_STARTS = (
    "flowchart", "graph", "sequencediagram", "classdiagram", "statediagram",
    "erdiagram", "journey", "gantt", "pie", "mindmap", "timeline", "quadrantchart",
)

DIAGRAM_SYSTEM = f"""You are a technical illustrator who draws diagrams as Mermaid code.

Propose 0–{MAX_DIAGRAMS} diagrams that materially improve understanding: architecture, data flow,
request/response sequences, state machines, decision flows, pipelines. Never decorative.
If the article would not benefit, return an empty list.

For each diagram:
- section_title: copy EXACTLY one of the provided section titles. The diagram is placed at the end of it.
  At most one diagram per section.
- title: short name. caption: one sentence explaining what the reader should notice.
- mermaid: valid Mermaid source only (no ``` fences). Prefer `flowchart LR`/`flowchart TD`,
  `sequenceDiagram`, `stateDiagram-v2`, `classDiagram` or `erDiagram`.

Mermaid syntax rules (avoid common breakages):
- Node IDs are simple alphanumerics (A, B1, apiGateway). Put labels in brackets: A["Label text"].
- Wrap every label in double quotes; never put double quotes or square brackets inside a label.
- Edge labels: A -->|"label"| B.
- Max ~15 nodes, labels of at most 5 words. No styling, classDef or click directives.
- The diagram must reflect what the article actually says.
"""


def _clean_mermaid(code: str) -> Optional[str]:
    text = (code or "").strip()
    text = re.sub(r"^```(?:mermaid)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()
    first = text.split(None, 1)[0].lower() if text else ""
    if not first or not first.startswith(MERMAID_STARTS):
        return None
    if re.search(r"^\s*(click|classDef|style)\b", text, flags=re.M | re.I):
        text = "\n".join(
            line for line in text.splitlines()
            if not re.match(r"^\s*(click|classDef|style)\b", line, flags=re.I)
        )
    return text


def _heading_key(text: str) -> str:
    text = re.sub(r"^\s*(\d+[\.\)]\s*)", "", text or "")
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def diagrams_node(state: State, config: RunnableConfig) -> dict:
    md = state["merged_md"]
    lines = md.split("\n")

    # H2 headings outside code fences.
    headings: List[tuple[int, str]] = []
    in_code = False
    for index, line in enumerate(lines):
        if line.strip().startswith("```"):
            in_code = not in_code
        elif not in_code and line.startswith("## "):
            headings.append((index, line[3:].strip()))

    if not headings:
        return {"md_with_diagrams": md, "diagrams": []}

    try:
        plan: DiagramPlan = invoke_structured(
            config,
            DiagramPlan,
            [
                SystemMessage(content=DIAGRAM_SYSTEM),
                HumanMessage(
                    content=(
                        "Section titles:\n"
                        + "\n".join(f"- {title}" for _, title in headings)
                        + f"\n\nArticle:\n{md[:24000]}"
                    )
                ),
            ],
        )
    except RuntimeError as error:
        logger.warning("Diagram planning failed, skipping diagrams: %s", error)
        return {"md_with_diagrams": md, "diagrams": []}

    by_key = {_heading_key(title): (index, title) for index, title in headings}
    chosen: Dict[int, dict] = {}
    for spec in plan.diagrams:
        if len(chosen) >= MAX_DIAGRAMS:
            break
        code = _clean_mermaid(spec.mermaid)
        match = by_key.get(_heading_key(spec.section_title))
        if not code or not match or match[0] in chosen:
            continue
        chosen[match[0]] = {
            "section_title": match[1],
            "title": spec.title.strip(),
            "caption": spec.caption.strip(),
            "mermaid": code,
        }

    # Insert each diagram at the end of its section (before the next H2), bottom-up.
    heading_lines = [index for index, _ in headings]
    ordered = sorted(chosen.items())
    for figure_number, (_, spec) in enumerate(ordered, start=1):
        spec["figure"] = figure_number

    for heading_index, spec in reversed(ordered):
        position = heading_lines.index(heading_index)
        end = heading_lines[position + 1] if position + 1 < len(heading_lines) else len(lines)
        while end > heading_index + 1 and not lines[end - 1].strip():
            end -= 1
        block = [
            "",
            "```mermaid",
            spec["mermaid"],
            "```",
            "",
            f"*Figure {spec['figure']}: {spec['caption'] or spec['title']}*",
        ]
        lines[end:end] = block

    return {
        "md_with_diagrams": "\n".join(lines),
        "diagrams": [spec for _, spec in ordered],
    }


# =============================================================
# 12) SEO / publishing agent
# =============================================================
SEO_SYSTEM = """You are an SEO editor preparing a technical article for publishing on a blog,
GitHub Pages and dev.to.

Produce:
- seo_title: compelling, specific, max 60 characters.
- meta_description: 140–160 characters, states the concrete benefit to the reader.
- slug: lowercase, hyphen-separated, 3–8 words, no stop-word padding.
- keywords: 5–10 realistic search phrases.
- tags: 3–5 single-word or hyphenated lowercase tags.
- summary: 2–3 sentence TL;DR.
- social_post: max 280 characters, no hashtag spam (max 2), no emojis overload.
Do not promise anything the article does not deliver.
"""


def seo_node(state: State, config: RunnableConfig) -> dict:
    plan = _plan(state)
    md = state.get("md_with_diagrams") or state["merged_md"]

    try:
        seo: SeoPack = invoke_structured(
            config,
            SeoPack,
            [
                SystemMessage(content=SEO_SYSTEM),
                HumanMessage(
                    content=(
                        f"Topic: {state['topic']}\nAudience: {plan.audience}\n"
                        f"Title: {plan.blog_title}\n\nArticle:\n{md[:12000]}"
                    )
                ),
            ],
        )
        data = seo.model_dump()
    except RuntimeError as error:
        logger.warning("SEO generation failed, using fallback: %s", error)
        data = {
            "seo_title": plan.blog_title,
            "meta_description": plan.tasks[0].goal if plan.tasks else plan.blog_title,
            "slug": plan.blog_title,
            "keywords": [],
            "tags": [],
            "summary": "",
            "social_post": "",
        }

    data["slug"] = _slugify(data.get("slug") or plan.blog_title)
    data["seo_title"] = (data.get("seo_title") or plan.blog_title).strip()[:70]
    data["meta_description"] = (data.get("meta_description") or "").strip()[:160]
    data["tags"] = [_slugify(t, 30) for t in data.get("tags") or [] if t.strip()][:5]
    data["keywords"] = [k.strip() for k in data.get("keywords") or [] if k.strip()][:10]
    return {"seo": data}


# =============================================================
# 13) Finalize: write outputs/<run_id>/
# =============================================================
def finalize_node(state: State, config: RunnableConfig) -> dict:
    md = state.get("md_with_diagrams") or state["merged_md"]
    run_id = str((config.get("configurable") or {}).get("thread_id") or "local")
    run_id = re.sub(r"[^A-Za-z0-9_-]", "", run_id) or "local"

    run_dir = OUTPUTS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "blog.md").write_text(md, encoding="utf-8")
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "topic": state.get("topic"),
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "mode": state.get("mode"),
                "seo": state.get("seo") or {},
                "review": state.get("review") or {},
                "citations": state.get("citation_report") or {},
                "diagrams": state.get("diagrams") or [],
                "sources": state.get("evidence") or [],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {"final": md, "output_path": str(run_dir / "blog.md")}


# =============================================================
# 14) Graph
# =============================================================
g = StateGraph(State)
g.add_node("router", router_node)
g.add_node("research", research_node)
g.add_node("orchestrator", orchestrator_node)
g.add_node("human_review", human_review_node)
g.add_node("worker", worker_node)
g.add_node("editor_review", editor_review_node)
g.add_node("reviser", reviser_node)
g.add_node("merge", merge_node)
g.add_node("citation_check", citation_check_node)
g.add_node("diagrams", diagrams_node)
g.add_node("seo", seo_node)
g.add_node("finalize", finalize_node)

g.add_edge(START, "router")
g.add_conditional_edges("router", route_next, {"research": "research", "orchestrator": "orchestrator"})
g.add_edge("research", "orchestrator")
g.add_edge("orchestrator", "human_review")
g.add_conditional_edges("human_review", after_review, ["orchestrator", "worker"])
g.add_edge("worker", "editor_review")
g.add_conditional_edges("editor_review", after_editor, ["reviser", "merge"])
g.add_conditional_edges("reviser", after_reviser, ["editor_review", "merge"])
g.add_edge("merge", "citation_check")
g.add_edge("citation_check", "diagrams")
g.add_edge("diagrams", "seo")
g.add_edge("seo", "finalize")
g.add_edge("finalize", END)


# =============================================================
# 15) Checkpointer: pooled Postgres, or in-memory fallback
# =============================================================
def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        return ""
    host = urlparse(url).hostname or ""
    if "sslmode=" not in url and host not in {"localhost", "127.0.0.1", "::1"}:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


def build_checkpointer():
    url = _database_url()
    if not url:
        logger.warning("DATABASE_URL not set; using in-memory checkpoints (lost on restart).")
        return "memory", InMemorySaver()

    try:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
        from langgraph.checkpoint.postgres import PostgresSaver

        pool = ConnectionPool(
            conninfo=url,
            min_size=1,
            max_size=int(os.getenv("DB_POOL_SIZE", "10")),
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
            open=True,
        )
        pool.wait(timeout=15)
        saver = PostgresSaver(pool)
        saver.setup()
        return "postgres", saver
    except Exception as error:  # noqa: BLE001
        logger.error("Postgres checkpointer unavailable (%s); using in-memory checkpoints.", error)
        return "memory", InMemorySaver()


CHECKPOINTER_KIND, checkpointer = build_checkpointer()
app = g.compile(checkpointer=checkpointer)
