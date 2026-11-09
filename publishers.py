"""
Publish generated articles to GitHub Pages (Jekyll) and dev.to.

Credentials are supplied by the user per request (their own GitHub repo/token and
dev.to API key). Nothing is read from server configuration and nothing is stored.

GitHub Pages is published first so its URL can be the canonical URL of the
dev.to copy (avoids duplicate-content SEO penalties).
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Dict, List, Optional

import requests

TIMEOUT = 30
USER_AGENT = "AgentWriter-AI"
PLATFORMS = ("github", "devto")
MERMAID_BLOCK_RE = re.compile(r"```mermaid\n(.*?)\n```", re.S)
REPO_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")


class PublishError(Exception):
    pass


@dataclass
class GithubCredentials:
    token: str
    repo: str
    branch: str = "main"
    base_url: str = ""

    def validate(self) -> None:
        if not self.token:
            raise PublishError("GitHub token is missing.")
        if not REPO_RE.match(self.repo):
            raise PublishError("GitHub repo must look like 'username/username.github.io'.")
        if not BRANCH_RE.match(self.branch or "main") or ".." in self.branch:
            raise PublishError("Invalid GitHub branch name.")
        if self.base_url and not re.match(r"^https://[^\s/]+(/[^\s]*)?$", self.base_url):
            raise PublishError("Site URL must start with https://")


@dataclass
class DevtoCredentials:
    api_key: str

    def validate(self) -> None:
        if not self.api_key:
            raise PublishError("dev.to API key is missing.")


def _fingerprint(secret: str) -> str:
    """Non-reversible id so a later call can tell whether it's the same dev.to account."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------
# Markdown transforms
# ---------------------------------------------------------
def split_title(markdown: str) -> tuple[str, str]:
    """Return (title, body) where body has the leading '# Title' line removed."""
    match = re.match(r"\s*# ([^\n]+)\n+", markdown)
    if not match:
        return "", markdown.strip()
    return match.group(1).strip(), markdown[match.end():].strip()


def mermaid_to_images(markdown: str) -> str:
    """Replace ```mermaid blocks with rendered PNGs (for platforms without Mermaid)."""

    def replace(match: re.Match) -> str:
        token = base64.urlsafe_b64encode(match.group(1).encode("utf-8")).decode("ascii")
        return f"![Diagram](https://mermaid.ink/img/{token}?type=png&bgColor=white)"

    return MERMAID_BLOCK_RE.sub(replace, markdown)


def mermaid_to_html(markdown: str) -> str:
    """Replace ```mermaid blocks with <pre class="mermaid"> rendered by mermaid.js (Jekyll)."""
    found = False

    def replace(match: re.Match) -> str:
        nonlocal found
        found = True
        return f'<pre class="mermaid">\n{html.escape(match.group(1), quote=False)}\n</pre>'

    output = MERMAID_BLOCK_RE.sub(replace, markdown)
    if found:
        output += (
            '\n\n<script type="module">\n'
            '  import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.esm.min.mjs";\n'
            "  mermaid.initialize({ startOnLoad: true });\n"
            "</script>\n"
        )
    return output


def _check(response: requests.Response, platform: str) -> Any:
    if response.status_code in (401, 403):
        raise PublishError(f"{platform} rejected the credentials (HTTP {response.status_code}). Check the token and its permissions.")
    if response.status_code == 404 and platform == "GitHub":
        raise PublishError("GitHub repo or branch not found, or the token has no access to it.")
    if response.status_code >= 400:
        detail = response.text[:300]
        try:
            body = response.json()
            detail = body.get("error") or body.get("message") or detail
        except ValueError:
            pass
        raise PublishError(f"{platform} returned HTTP {response.status_code}: {detail}")
    try:
        return response.json()
    except ValueError as error:
        raise PublishError(f"{platform} returned a non-JSON response.") from error


# ---------------------------------------------------------
# GitHub Pages (Jekyll)
# ---------------------------------------------------------
def _github_headers(creds: GithubCredentials) -> dict:
    return {
        "Authorization": f"Bearer {creds.token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }


def github_site_url(creds: GithubCredentials) -> str:
    if creds.base_url:
        return creds.base_url.rstrip("/")
    owner, _, name = creds.repo.partition("/")
    if name.lower() == f"{owner.lower()}.github.io":
        return f"https://{owner.lower()}.github.io"
    return f"https://{owner.lower()}.github.io/{name}"


def _github_put(creds: GithubCredentials, path: str, content: str, message: str) -> dict:
    url = f"https://api.github.com/repos/{creds.repo}/contents/{path}"
    headers = _github_headers(creds)
    existing = requests.get(url, headers=headers, params={"ref": creds.branch}, timeout=TIMEOUT)
    if existing.status_code in (401, 403):
        _check(existing, "GitHub")
    payload: Dict[str, Any] = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": creds.branch,
    }
    if existing.status_code == 200:
        payload["sha"] = existing.json().get("sha")
    response = requests.put(url, headers=headers, json=payload, timeout=TIMEOUT)
    return _check(response, "GitHub")


def _github_delete(creds: GithubCredentials, path: str, message: str) -> None:
    url = f"https://api.github.com/repos/{creds.repo}/contents/{path}"
    headers = _github_headers(creds)
    existing = requests.get(url, headers=headers, params={"ref": creds.branch}, timeout=TIMEOUT)
    if existing.status_code != 200:
        return
    requests.delete(
        url,
        headers=headers,
        json={"message": message, "sha": existing.json().get("sha"), "branch": creds.branch},
        timeout=TIMEOUT,
    )


def publish_github(article: dict, draft: bool, creds: GithubCredentials, previous: Optional[dict] = None) -> dict:
    slug = article["slug"]
    today = date.today().isoformat()
    permalink = f"/posts/{slug}/"
    path = f"_drafts/{slug}.md" if draft else f"_posts/{today}-{slug}.md"

    front_matter = {
        "layout": "post",
        "title": article["title"],
        "date": today,
        "description": article["description"],
        "tags": article["tags"],
        "permalink": permalink,
    }
    content = (
        "---\n"
        + "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in front_matter.items())
        + "\n---\n\n"
        + mermaid_to_html(article["body"])
        + "\n"
    )

    result = _github_put(creds, path, content, f"{'Draft' if draft else 'Publish'}: {article['title']}")

    # Promoting a draft: remove the old _drafts copy.
    old_path = (previous or {}).get("path")
    if not draft and old_path and old_path.startswith("_drafts/") and old_path != path:
        _github_delete(creds, old_path, f"Remove draft: {article['title']}")

    file_url = (result.get("content") or {}).get("html_url", "")
    return {
        "url": file_url if draft else github_site_url(creds) + permalink,
        "file_url": file_url,
        "path": path,
        "account": creds.repo.lower(),
        "note": "Saved in _drafts (not public)." if draft else "GitHub Pages usually rebuilds within 1–2 minutes.",
    }


# ---------------------------------------------------------
# dev.to (Forem API)
# ---------------------------------------------------------
def publish_devto(
    article: dict,
    draft: bool,
    canonical_url: Optional[str],
    creds: DevtoCredentials,
    previous: Optional[dict] = None,
) -> dict:
    headers = {
        "api-key": creds.api_key,
        "Accept": "application/vnd.forem.api-v1+json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    # dev.to tags: max 4, lowercase alphanumerics only.
    tags = [re.sub(r"[^a-z0-9]", "", t.lower()) for t in article["tags"]]
    payload = {
        "article": {
            "title": article["title"],
            "body_markdown": mermaid_to_images(article["body"]),
            "published": not draft,
            "tags": ",".join([t for t in tags if t][:4]),
            "description": article["description"],
            "canonical_url": canonical_url or None,
        }
    }

    previous_id = (previous or {}).get("id")
    if previous_id:
        # Update the existing article (e.g. promote a draft) instead of duplicating it.
        response = requests.put(
            f"https://dev.to/api/articles/{previous_id}", headers=headers, json=payload, timeout=TIMEOUT
        )
    else:
        response = requests.post("https://dev.to/api/articles", headers=headers, json=payload, timeout=TIMEOUT)

    data = _check(response, "dev.to")
    return {
        "id": data.get("id"),
        "url": data.get("url") or "https://dev.to/dashboard",
        "account": _fingerprint(creds.api_key),
        "note": "Saved as a draft on your dev.to dashboard." if draft else "",
    }


# ---------------------------------------------------------
# Orchestration
# ---------------------------------------------------------
def build_article(markdown: str, seo: dict) -> dict:
    title, body = split_title(markdown)
    title = title or seo.get("seo_title") or "Untitled"
    return {
        "title": title,
        "body": body,
        "slug": seo.get("slug") or re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "article",
        "description": seo.get("meta_description") or "",
        "tags": [t for t in seo.get("tags") or [] if t][:5],
    }


def publish_article(
    markdown: str,
    seo: dict,
    platforms: List[str],
    draft: bool,
    github: Optional[GithubCredentials] = None,
    devto: Optional[DevtoCredentials] = None,
    previous: Optional[Dict[str, dict]] = None,
) -> Dict[str, dict]:
    previous = previous or {}
    article = build_article(markdown, seo)
    mode = "draft" if draft else "publish"
    accounts = {
        "github": github.repo.lower() if github else None,
        "devto": _fingerprint(devto.api_key) if devto and devto.api_key else None,
    }
    results: Dict[str, dict] = {}

    def earlier(platform: str) -> dict:
        """Previous result for this run, only if it went to the same account."""
        prev = previous.get(platform) or {}
        return prev if prev.get("ok") and prev.get("account") == accounts[platform] else {}

    # Canonical URL: the public GitHub Pages copy, from this call or an earlier one.
    canonical: Optional[str] = None
    if earlier("github").get("mode") == "publish":
        canonical = earlier("github").get("url")

    for platform in PLATFORMS:
        if platform not in platforms:
            continue
        prev = earlier(platform)

        if prev.get("mode") == "publish":
            results[platform] = {**prev, "skipped": True, "note": "Already published for this article."}
            continue
        if prev.get("mode") == mode == "draft":
            results[platform] = {**prev, "skipped": True, "note": "Draft already created for this article."}
            continue

        publishers: Dict[str, Callable[[], dict]] = {
            "github": lambda: publish_github(article, draft, github, prev),
            "devto": lambda: publish_devto(article, draft, canonical, devto, prev),
        }
        try:
            creds = github if platform == "github" else devto
            if creds is None:
                raise PublishError("Credentials are missing.")
            creds.validate()
            outcome = publishers[platform]()
            results[platform] = {"ok": True, "mode": mode, **outcome}
            if platform == "github" and not draft:
                canonical = outcome["url"]
        except (PublishError, requests.RequestException) as error:
            results[platform] = {"ok": False, "mode": mode, "error": str(error)}

    return results
