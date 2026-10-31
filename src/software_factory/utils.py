import json
import mimetypes
from pathlib import Path
import httpx
import re

CONVERSATION_ID_RE = re.compile(
    r"\{\{\s*conversation_id\s*:\s*([0-9a-fA-F-]{36})\s*\}\}"
)

UPLOAD_URL_RE = re.compile(r"https://uploads\.linear\.app/[^\s)\]>\"']+")


def extract_conversation_id(text: str) -> str | None:
    match = CONVERSATION_ID_RE.search(text)
    return match.group(1) if match else None

def extract_upload_urls(text: str) -> list[str]:
    # dict.fromkeys dedupes while keeping order
    return list(dict.fromkeys(UPLOAD_URL_RE.findall(text)))


def download_uploads(urls: list[str], dest: Path, api_key: str) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    paths = []
    with httpx.Client(follow_redirects=True, headers={"Authorization": api_key}, verify=False) as client:
        for i, url in enumerate(urls):
            resp = client.get(url)
            resp.raise_for_status()
            ext = mimetypes.guess_extension(resp.headers.get("content-type", "").split(";")[0]) or ""
            path = dest / f"attachment_{i}{ext}"
            path.write_bytes(resp.content)
            paths.append(path)
    return paths


def write_conversation_json(conversation: dict, path: Path) -> Path:
    """Save a cleaned (and redacted) conversation for the agent to query beyond the markdown."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(conversation, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
