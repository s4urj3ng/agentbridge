"""Minimal OpenAI-compatible chat-completions client run as a subprocess.

Reads the prompt from stdin, the API key from AB_HTTP_API_KEY, prints
{"text", "structured", "usage", "credits_remaining"} JSON on stdout. Exit 1 on
HTTP errors with the status (and Retry-After, if any) on stderr for
classification.

Mindlogic Gateway specifics (docs.mindlogic.ai, API Gateway reference):
  * paths end with a slash: POST {base}/chat/completions/
  * GET {base}/credits/ returns the real balance; with --credit-floor the call
    is refused (reported as a usage limit) once total.remaining drops below it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import httpx


def endpoint(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + "/" + path.strip("/") + "/"


def credits_remaining(client: httpx.Client, base_url: str, key: str) -> float | None:
    try:
        r = client.get(endpoint(base_url, "credits"), headers={"Authorization": f"Bearer {key}"}, timeout=30)
        if r.status_code == 200:
            return float(r.json()["total"]["remaining"])
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        pass
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--credit-floor", type=float, default=None)
    ap.add_argument("--max-tokens", type=int, default=None)
    a = ap.parse_args()
    key = os.environ.get("AB_HTTP_API_KEY")
    if not key or not a.base_url:
        print("authentication required: API key or base URL missing", file=sys.stderr)
        return 1
    prompt = sys.stdin.read()
    with httpx.Client(follow_redirects=True) as client:
        remaining = None
        if a.credit_floor is not None:
            remaining = credits_remaining(client, a.base_url, key)
            if remaining is None:
                print("credit balance unavailable; refusing call because a credit floor is configured",
                      file=sys.stderr)
                return 1
            if remaining < a.credit_floor:
                print(f"usage limit reached: {remaining:.1f} credits remaining, floor {a.credit_floor}",
                      file=sys.stderr)
                return 1
        body = {"model": a.model, "messages": [{"role": "user", "content": prompt}]}
        if a.max_tokens:
            body["max_tokens"] = a.max_tokens
        r = client.post(endpoint(a.base_url, "chat/completions"), headers={"Authorization": f"Bearer {key}"},
                        json=body, timeout=600)
        if r.status_code != 200:
            ra = r.headers.get("retry-after")
            print(f"HTTP {r.status_code}{' retry-after: ' + ra if ra else ''} {r.text[:500]}", file=sys.stderr)
            return 1
        d = r.json()
        if remaining is not None:
            remaining = credits_remaining(client, a.base_url, key)
    text = d["choices"][0]["message"]["content"] or ""
    structured = None
    try:
        from agentbridge.orchestrator.decisions import _extract_json
        v = _extract_json(text)
        structured = v if isinstance(v, dict) else None
    except Exception:
        pass
    print(json.dumps({"text": text, "structured": structured, "usage": d.get("usage") or {},
                      "credits_remaining": remaining}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
