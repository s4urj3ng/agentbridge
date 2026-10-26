"""Minimal OpenAI-compatible chat-completions client run as a subprocess.

Reads the prompt from stdin, the API key from AB_HTTP_API_KEY, prints
{"text", "structured", "usage"} JSON on stdout. Exit 1 on HTTP errors with
the status (and Retry-After, if any) on stderr for classification.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import httpx


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--model", required=True)
    a = ap.parse_args()
    key = os.environ.get("AB_HTTP_API_KEY")
    if not key or not a.base_url:
        print("authentication required: API key or base URL missing", file=sys.stderr)
        return 1
    prompt = sys.stdin.read()
    r = httpx.post(a.base_url.rstrip("/") + "/chat/completions",
                   headers={"Authorization": f"Bearer {key}"},
                   json={"model": a.model, "messages": [{"role": "user", "content": prompt}]}, timeout=600)
    if r.status_code != 200:
        ra = r.headers.get("retry-after")
        print(f"HTTP {r.status_code}{' retry-after: ' + ra if ra else ''} {r.text[:500]}", file=sys.stderr)
        return 1
    d = r.json()
    text = d["choices"][0]["message"]["content"] or ""
    structured = None
    try:
        from agentbridge.orchestrator.decisions import _extract_json
        v = _extract_json(text)
        structured = v if isinstance(v, dict) else None
    except Exception:
        pass
    print(json.dumps({"text": text, "structured": structured, "usage": d.get("usage") or {}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
