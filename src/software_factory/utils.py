import json
import mimetypes
import os
import ssl
from pathlib import Path
import httpx
import re

CONVERSATION_ID_RE = re.compile(
    r"\{\{\s*conversation_id\s*:\s*([0-9a-fA-F-]{36})\s*\}\}"
)

UPLOAD_URL_RE = re.compile(r"https://uploads\.linear\.app/[^\s)\]>\"']+")


def ssl_context() -> ssl.SSLContext:
    """Verify TLS against the system store, which trusts the proxy that re-signs our traffic.

    httpx's default (certifi) doesn't include the proxy's CA. Set SF_CA_BUNDLE to a PEM
    file with that CA where the system store lacks it, e.g. in WSL or a container.
    """
    context = ssl.create_default_context()
    if ca_bundle := os.getenv("SF_CA_BUNDLE"):
        context.load_verify_locations(cafile=ca_bundle)
    return context


def extract_conversation_id(text: str) -> str | None:
    match = CONVERSATION_ID_RE.search(text)
    return match.group(1) if match else None

def extract_upload_urls(text: str) -> list[str]:
    # dict.fromkeys dedupes while keeping order
    return list(dict.fromkeys(UPLOAD_URL_RE.findall(text)))


def download_uploads(urls: list[str], dest: Path, api_key: str) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    paths = []
    with httpx.Client(follow_redirects=True, headers={"Authorization": api_key}, verify=ssl_context()) as client:
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
