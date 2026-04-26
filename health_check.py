# -*- coding: utf-8 -*-
"""
TraceBack pre-flight health check.

Usage:
    python health_check.py                    # check local bridge (must be running)
    python health_check.py --bridge-url http://127.0.0.1:8080
    python health_check.py --bridge-url https://my-traceback.fly.dev

Or run checks directly without a running bridge:
    python health_check.py --no-bridge

Exit codes:
    0 — all checks passed
    1 — one or more checks failed
"""
import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

import certifi
from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env", override=True)

GREEN = "\033[32m"
RED   = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"
BOLD  = "\033[1m"

SCHEMA_EMBEDDING_DIM = 768


def ok(label: str, detail: str = "") -> None:
    suffix = f"  {detail}" if detail else ""
    print(f"  {GREEN}✓{RESET} {label}{suffix}")


def fail(label: str, detail: str = "") -> None:
    suffix = f"\n      {detail}" if detail else ""
    print(f"  {RED}✗{RESET} {label}{suffix}")


def warn(label: str, detail: str = "") -> None:
    suffix = f"  {detail}" if detail else ""
    print(f"  {YELLOW}~{RESET} {label}{suffix}")


def check_env() -> bool:
    print(f"\n{BOLD}[1/4] Environment variables{RESET}")
    provider = os.getenv("EMBEDDING_PROVIDER", "gemini").lower()
    required = {
        "SUPABASE_URL": os.getenv("SUPABASE_URL", ""),
        "SUPABASE_KEY": os.getenv("SUPABASE_KEY", ""),
        "ASI1_API_KEY": os.getenv("ASI1_API_KEY", ""),
    }
    if provider == "gemini":
        required["GEMINI_API_KEY"] = os.getenv("GEMINI_API_KEY", "")
    elif provider == "openai":
        required["OPENAI_API_KEY"] = os.getenv("OPENAI_API_KEY", "")

    passed = True
    for var, val in required.items():
        if val:
            ok(var, f"({val[:8]}...)")
        else:
            fail(var, "not set — add it to your .env file")
            passed = False

    print(f"      EMBEDDING_PROVIDER = {provider}")
    print(f"      SCHEMA_EMBEDDING_DIM = {SCHEMA_EMBEDDING_DIM}")
    return passed


def check_embedding() -> bool:
    print(f"\n{BOLD}[2/4] Embedding provider{RESET}")
    provider = os.getenv("EMBEDDING_PROVIDER", "gemini").lower()

    if provider == "gemini":
        key = os.getenv("GEMINI_API_KEY", "")
        if not key:
            fail("Gemini API", "GEMINI_API_KEY not set")
            return False
        model = os.getenv("GEMINI_EMBEDDING_MODEL", "models/text-embedding-004")
        if not model.startswith("models/"):
            model = f"models/{model}"
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/"
            f"{model}:embedContent?key={key}"
        )
        body = json.dumps({
            "model": model,
            "content": {"parts": [{"text": "health check"}]},
            "outputDimensionality": SCHEMA_EMBEDDING_DIM,
        }).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        ctx = ssl.create_default_context(cafile=certifi.where())
        try:
            with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
                values = json.loads(r.read())["embedding"]["values"]
            dim = len(values)
            if dim == SCHEMA_EMBEDDING_DIM:
                ok(f"Gemini {model}", f"→ {dim}-dim  ✓ matches schema")
                return True
            else:
                fail(
                    f"Gemini {model}",
                    f"returned {dim}-dim, schema expects {SCHEMA_EMBEDDING_DIM}-dim. "
                    f"Re-run database/schema.sql with vector({dim}) OR switch to "
                    f"GEMINI_EMBEDDING_MODEL=models/text-embedding-004.",
                )
                return False
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="ignore")
            if e.code in (401, 403):
                fail(f"Gemini API auth (HTTP {e.code})", f"Check GEMINI_API_KEY: {detail[:200]}")
            else:
                fail(f"Gemini API (HTTP {e.code})", detail[:200])
            return False
        except Exception as e:
            fail("Gemini API", str(e))
            return False

    elif provider == "openai":
        try:
            from openai import OpenAI
            client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
            resp = client.embeddings.create(
                model="text-embedding-3-small",
                input="health check",
                dimensions=SCHEMA_EMBEDDING_DIM,
            )
            dim = len(resp.data[0].embedding)
            if dim == SCHEMA_EMBEDDING_DIM:
                ok("OpenAI text-embedding-3-small", f"→ {dim}-dim  ✓ matches schema")
                return True
            else:
                fail("OpenAI", f"returned {dim}-dim, schema expects {SCHEMA_EMBEDDING_DIM}-dim")
                return False
        except Exception as e:
            fail("OpenAI", str(e))
            return False
    else:
        fail("Embedding provider", f"Unknown EMBEDDING_PROVIDER={provider!r}")
        return False


def check_supabase() -> bool:
    print(f"\n{BOLD}[3/4] Supabase connectivity{RESET}")
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_KEY", "")
    if not url or not key:
        fail("Supabase", "SUPABASE_URL or SUPABASE_KEY not set")
        return False
    try:
        from supabase import create_client
        sb = create_client(url, key)
        result = sb.table("code_chunks").select("id", count="exact").limit(1).execute()
        count = result.count or 0
        ok("code_chunks table reachable", f"({count} chunks indexed)")
        return True
    except Exception as e:
        err = str(e)
        low = err.lower()
        if any(x in low for x in ("401", "403", "jwt", "apikey", "authentication")):
            fail("Supabase auth", f"Check SUPABASE_KEY — RLS or invalid key: {err}")
        elif "does not exist" in low or "relation" in low:
            fail("code_chunks table missing", "Run database/schema.sql in Supabase SQL editor")
        else:
            fail("Supabase", err)
        return False


def check_rpc() -> bool:
    print(f"\n{BOLD}[4/4] Supabase RPC (match_code_chunks){RESET}")
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_KEY", "")
    if not url or not key:
        fail("Supabase RPC", "credentials not set — skipping")
        return False
    try:
        from supabase import create_client
        sb = create_client(url, key)
        zero_vec = [0.0] * SCHEMA_EMBEDDING_DIM
        sb.rpc("match_code_chunks", {"query_embedding": zero_vec, "match_count": 1}).execute()
        ok("match_code_chunks()", f"callable with {SCHEMA_EMBEDDING_DIM}-dim vector")
        return True
    except Exception as e:
        err = str(e)
        low = err.lower()
        if "not exist" in low or "schema cache" in low:
            fail("match_code_chunks() not found", "Run database/schema.sql in Supabase SQL editor")
        elif "dimension" in low:
            fail(
                "Dimension mismatch in RPC",
                f"schema.sql was created with a different vector size. "
                f"Re-run database/schema.sql (vector({SCHEMA_EMBEDDING_DIM})) and reindex.",
            )
        else:
            fail("RPC call failed", err)
        return False


def check_bridge(bridge_url: str) -> bool:
    print(f"\n{BOLD}[+] Bridge health endpoint{RESET}  ({bridge_url}/health)")
    req = urllib.request.Request(f"{bridge_url}/health")
    ctx = ssl.create_default_context(cafile=certifi.where())
    try:
        with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
            data = json.loads(r.read())
    except Exception as e:
        fail("Bridge unreachable", str(e))
        return False

    all_ok = data.get("ok", False)
    for name, check in data.get("checks", {}).items():
        if check.get("ok"):
            ok(f"bridge/{name}")
        else:
            fail(f"bridge/{name}", check.get("error", "unknown error"))

    return all_ok


def main():
    parser = argparse.ArgumentParser(description="TraceBack health check")
    parser.add_argument("--bridge-url", default="", help="Bridge URL to ping (optional)")
    parser.add_argument(
        "--no-bridge",
        action="store_true",
        help="Skip bridge ping; run direct checks only",
    )
    args = parser.parse_args()

    print(f"{BOLD}TraceBack Health Check{RESET}")
    print("=" * 40)

    results = [
        check_env(),
        check_embedding(),
        check_supabase(),
        check_rpc(),
    ]

    if not args.no_bridge:
        bridge_url = (args.bridge_url or "http://127.0.0.1:8080").rstrip("/")
        results.append(check_bridge(bridge_url))

    print("\n" + "=" * 40)
    if all(results):
        print(f"{GREEN}{BOLD}All checks passed ✓{RESET}")
        sys.exit(0)
    else:
        failed = sum(1 for r in results if not r)
        print(f"{RED}{BOLD}{failed} check(s) failed — see details above.{RESET}")
        sys.exit(1)


if __name__ == "__main__":
    main()
