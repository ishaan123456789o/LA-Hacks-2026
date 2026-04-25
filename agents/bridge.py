"""
Bridge server — FastAPI on port 8080 (override with BRIDGE_PORT env var).
Spawns all three uagents on startup, serves VS Code extension REST API.

Canonical embedding dimension: SCHEMA_EMBEDDING_DIM = 768  (matches database/schema.sql)
Supported providers: gemini (default), openai
"""
import ast
import atexit
import json
import os
import re
import signal
import ssl
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

import certifi
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI
from pydantic import BaseModel
from supabase import create_client

AGENTS_DIR = Path(__file__).parent
ROOT_DIR = AGENTS_DIR.parent
load_dotenv(ROOT_DIR / ".env", override=True)

BRIDGE_PORT = int(os.getenv("BRIDGE_PORT", "8080"))

# ── Canonical embedding dimension — must match database/schema.sql vector(N) ──
# text-embedding-004 (Gemini) → 768-dim  ✓
# text-embedding-3-small (OpenAI) → 768-dim when dimensions=768 is passed  ✓
# gemini-embedding-exp-03-07 → natively 3072-dim, use outputDimensionality=768  ✓
SCHEMA_EMBEDDING_DIM = 768


# ── Lazy client initialisation (fail at call-time with clear message) ─────────

_supabase = None
_asi1_client = None


def _get_supabase():
    global _supabase
    if _supabase is None:
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_KEY", "")
        if not url or not key:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_KEY must be set in your .env file. "
                "Copy .env.example → .env and fill in your Supabase project credentials."
            )
        _supabase = create_client(url, key)
    return _supabase


def _get_asi1():
    global _asi1_client
    if _asi1_client is None:
        key = os.environ.get("ASI1_API_KEY", "")
        if not key:
            raise RuntimeError(
                "ASI1_API_KEY must be set in your .env file. "
                "Get your key from https://api.asi1.ai"
            )
        _asi1_client = OpenAI(base_url="https://api.asi1.ai/v1", api_key=key)
    return _asi1_client


# ── Embedding ──────────────────────────────────────────────────────────────────

DEFAULT_GEMINI_EMBEDDING_MODELS = ("models/embedding-001", "models/text-embedding-004")
TARGET_VECTOR_DIM = int(os.getenv("TARGET_VECTOR_DIM", "768"))


def _normalize_vector(vector: list, target_dim: int = TARGET_VECTOR_DIM) -> list:
    values = list(vector)
    if len(values) == target_dim:
        return values
    if len(values) > target_dim:
        return values[:target_dim]
    return values + [0.0] * (target_dim - len(values))


def _gemini_request(url: str, body: dict | None, method: str = "POST") -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    ctx = ssl.create_default_context(cafile=certifi.where())
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as err:
        details = err.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Gemini request failed: {err.code} {details}") from err


def _discover_gemini_embedding_models(key: str) -> List[str]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
    try:
        payload = _gemini_request(url=url, body=None, method="GET")
    except RuntimeError:
        return []

    models: List[str] = []
    for item in payload.get("models", []):
        name = item.get("name")
        supported = item.get("supportedGenerationMethods", [])
        if name and "embedContent" in supported:
            models.append(name)
    return models


def _gemini_embed(text: str, key: str) -> list:
    configured_model = os.getenv("GEMINI_EMBEDDING_MODEL")
    candidate_models: List[str] = []
    if configured_model:
        candidate_models.append(configured_model)
    candidate_models.extend(DEFAULT_GEMINI_EMBEDDING_MODELS)
    candidate_models.extend(_discover_gemini_embedding_models(key))
    candidate_models = list(dict.fromkeys(candidate_models))

    last_error: Exception | None = None
    for model in candidate_models:
        normalized = model if model.startswith("models/") else f"models/{model}"
        url = f"https://generativelanguage.googleapis.com/v1beta/{normalized}:embedContent?key={key}"
        body = {"model": normalized, "content": {"parts": [{"text": text}]}}
        try:
            payload = _gemini_request(url=url, body=body)
        except RuntimeError as err:
            last_error = err
            continue

        values = payload.get("embedding", {}).get("values")
        if values:
            return values
        last_error = RuntimeError(f"Gemini embedding response missing values for {normalized}")

    raise RuntimeError(
        "Gemini embedding failed for all candidate models. "
        "Set GEMINI_EMBEDDING_MODEL to a model that supports embedContent."
    ) from last_error


def embed_text(text: str) -> list:
    provider = os.getenv("EMBEDDING_PROVIDER", "openai").lower()
    if provider == "gemini":
        key = os.environ["GEMINI_API_KEY"]
        return _normalize_vector(_gemini_embed(text=text, key=key))
    else:
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        resp = client.embeddings.create(model="text-embedding-3-small", input=text)
        return _normalize_vector(resp.data[0].embedding)


# ── Supabase error classifier ───────────────────────────────────────────────────

def _classify_supabase_error(e: Exception) -> HTTPException:
    """Convert Supabase / postgrest errors into actionable HTTP responses."""
    msg = str(e)
    low = msg.lower()

    if any(x in low for x in ("401", "403", "jwt", "authentication", "apikey", "unauthorized")):
        return HTTPException(
            status_code=401,
            detail=(
                f"Supabase auth error — verify SUPABASE_URL and SUPABASE_KEY in .env. "
                f"If using Row-Level Security, ensure the anon key has SELECT/INSERT "
                f"permissions on code_chunks: {msg}"
            ),
        )

    if "function" in low and ("not exist" in low or "does not exist" in low or "schema cache" in low):
        return HTTPException(
            status_code=503,
            detail=(
                "Supabase RPC 'match_code_chunks' not found. "
                "Run database/schema.sql in your Supabase SQL editor to create it, "
                "then retry. "
                f"Original error: {msg}"
            ),
        )

    if "dimension" in low or "expected" in low and "dimension" in low:
        return HTTPException(
            status_code=422,
            detail=(
                f"Vector dimension mismatch in Supabase. "
                f"Your database/schema.sql was created with a different vector size than "
                f"the current embedding model produces ({SCHEMA_EMBEDDING_DIM}-dim expected). "
                f"Migration steps: (1) Drop and re-create the table with the correct "
                f"vector({SCHEMA_EMBEDDING_DIM}) dimension, (2) re-run POST /index. "
                f"Original error: {msg}"
            ),
        )

    return HTTPException(status_code=500, detail=f"Supabase error: {msg}")


# ── AST repo parser ────────────────────────────────────────────────────────────

@dataclass
class _Chunk:
    file_path: str
    function_name: str
    raw_code: str


def _parse_file(file_path: str) -> List[_Chunk]:
    chunks: List[_Chunk] = []
    try:
        src = open(file_path, encoding="utf-8").read()
        tree = ast.parse(src)
        lines = src.splitlines()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                raw = "\n".join(lines[node.lineno - 1: node.end_lineno])
                chunks.append(_Chunk(file_path, node.name, raw))
    except Exception:
        pass
    return chunks


def _parse_repo(repo_path: str) -> List[_Chunk]:
    chunks: List[_Chunk] = []
    skip = {"venv", ".venv", "__pycache__", ".git", "node_modules", "dist", "build"}
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in skip]
        for fname in files:
            if not fname.endswith(".py"):
                continue
            chunks.extend(_parse_file(os.path.join(root, fname)))
    return chunks


# ── Agent subprocess launcher ──────────────────────────────────────────────────

_agent_procs: List[subprocess.Popen] = []


def _start_agents():
    python = sys.executable
    for script in ["parser_agent.py", "tracer_agent.py", "librarian.py"]:
        try:
            p = subprocess.Popen(
                [python, str(AGENTS_DIR / script)],
                cwd=str(ROOT_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            _agent_procs.append(p)
            print(f"[Bridge] Started {script} (pid {p.pid})", flush=True)
        except Exception as e:
            print(f"[Bridge] Failed to start {script}: {e}", flush=True)


def _cleanup():
    for p in _agent_procs:
        try:
            p.terminate()
        except Exception:
            pass


atexit.register(_cleanup)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

threading.Thread(target=_start_agents, daemon=True).start()


# ── FastAPI app ────────────────────────────────────────────────────────────────

app = FastAPI(title="TraceBack Bridge")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── API models ─────────────────────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    error_log: str


class IndexRequest(BaseModel):
    repo_path: str


class ReindexFileRequest(BaseModel):
    file_path: str


class FixRequest(BaseModel):
    error_log: str


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/health")
def health() -> Dict[str, Any]:
    """
    Full pre-flight check. Returns per-component status so the client can
    surface actionable diagnostics instead of a generic 'something went wrong'.

    Checks:
      env       — required environment variables are present
      embedding — provider is reachable and returns SCHEMA_EMBEDDING_DIM-dim vector
      supabase  — database is reachable (SELECT on code_chunks)
      rpc       — match_code_chunks() function exists and accepts the right dim
    """
    results: Dict[str, Any] = {}
    overall_ok = True

    # ── env vars ──────────────────────────────────────────────────────────────
    required = ["SUPABASE_URL", "SUPABASE_KEY", "ASI1_API_KEY"]
    provider = os.getenv("EMBEDDING_PROVIDER", "gemini").lower()
    if provider == "gemini":
        required.append("GEMINI_API_KEY")
    elif provider == "openai":
        required.append("OPENAI_API_KEY")

    missing = [v for v in required if not os.getenv(v)]
    env_ok = not missing
    results["env"] = {
        "ok": env_ok,
        "missing": missing,
        "embedding_provider": provider,
        "schema_embedding_dim": SCHEMA_EMBEDDING_DIM,
    }
    if not env_ok:
        overall_ok = False

    # ── embedding provider ────────────────────────────────────────────────────
    try:
        emb = embed_text("health check ping")
        results["embedding"] = {
            "ok": True,
            "dim": len(emb),
            "schema_dim": SCHEMA_EMBEDDING_DIM,
            "provider": provider,
        }
    except PermissionError as e:
        overall_ok = False
        results["embedding"] = {"ok": False, "error": str(e), "type": "auth"}
    except Exception as e:
        overall_ok = False
        results["embedding"] = {"ok": False, "error": str(e)}

    # ── supabase reachable ────────────────────────────────────────────────────
    try:
        _get_supabase().table("code_chunks").select("id", count="exact").limit(1).execute()
        results["supabase"] = {"ok": True}
    except Exception as e:
        overall_ok = False
        err = str(e)
        low = err.lower()
        if any(x in low for x in ("401", "403", "jwt", "authentication", "apikey")):
            results["supabase"] = {"ok": False, "error": err, "type": "auth"}
        else:
            results["supabase"] = {"ok": False, "error": err}

    # ── RPC callable ──────────────────────────────────────────────────────────
    try:
        zero_vec = [0.0] * SCHEMA_EMBEDDING_DIM
        _get_supabase().rpc(
            "match_code_chunks",
            {"query_embedding": zero_vec, "match_count": 1},
        ).execute()
        results["rpc"] = {"ok": True, "function": "match_code_chunks"}
    except Exception as e:
        overall_ok = False
        err = str(e)
        low = err.lower()
        if "not exist" in low or "schema cache" in low:
            results["rpc"] = {
                "ok": False,
                "error": err,
                "type": "rpc_missing",
                "fix": "Run database/schema.sql in Supabase SQL editor",
            }
        elif "dimension" in low:
            results["rpc"] = {
                "ok": False,
                "error": err,
                "type": "dim_mismatch",
                "fix": (
                    f"database/schema.sql was created with a different vector size. "
                    f"Re-run schema.sql (vector({SCHEMA_EMBEDDING_DIM})) and reindex."
                ),
            }
        else:
            results["rpc"] = {"ok": False, "error": err}

    return {"ok": overall_ok, "checks": results}


@app.get("/status")
def status():
    try:
        result = _get_supabase().table("code_chunks").select("id", count="exact").limit(1).execute()
        return {"indexed_chunks": result.count or 0, "ok": True}
    except Exception as e:
        return {"indexed_chunks": 0, "ok": False, "error": str(e)}


@app.post("/index")
def index_repo(req: IndexRequest):
    chunks = _parse_repo(req.repo_path)
    if not chunks:
        return {"status": "ok", "chunks": 0}

    sb = _get_supabase()
    try:
        sb.table("code_chunks").delete().like("file_path", f"{req.repo_path}%").execute()
    except Exception as e:
        raise _classify_supabase_error(e)

    BATCH = 20
    total = 0
    provider = os.getenv("EMBEDDING_PROVIDER", "gemini").lower()

    for start in range(0, len(chunks), BATCH):
        batch = chunks[start: start + BATCH]
        texts = [f"{c.file_path}::{c.function_name}\n{c.raw_code}" for c in batch]

        try:
            if provider == "gemini":
                embeddings = [embed_text(t) for t in texts]
            else:
                client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
                resp = client.embeddings.create(
                    model="text-embedding-3-small",
                    input=texts,
                    dimensions=SCHEMA_EMBEDDING_DIM,
                )
                embeddings = [r.embedding for r in resp.data]
        except PermissionError as e:
            raise HTTPException(status_code=401, detail=str(e))
        except (ValueError, RuntimeError) as e:
            raise HTTPException(status_code=500, detail=f"Embedding failed: {e}")

        rows = [
            {
                "file_path": batch[i].file_path,
                "function_name": batch[i].function_name,
                "raw_code": batch[i].raw_code,
                "embedding": embeddings[i],
            }
            for i in range(len(batch))
        ]
        try:
            sb.table("code_chunks").insert(rows).execute()
        except Exception as e:
            raise _classify_supabase_error(e)

        total += len(rows)
        print(f"[Bridge] Indexed {total}/{len(chunks)} chunks", flush=True)

    return {"status": "ok", "chunks": total}


@app.post("/reindex-file")
def reindex_file(req: ReindexFileRequest):
    sb = _get_supabase()
    try:
        sb.table("code_chunks").delete().eq("file_path", req.file_path).execute()
    except Exception as e:
        raise _classify_supabase_error(e)

    chunks = _parse_file(req.file_path)
    if not chunks:
        return {"status": "ok", "chunks": 0}

    provider = os.getenv("EMBEDDING_PROVIDER", "gemini").lower()
    BATCH = 20
    total = 0

    for start in range(0, len(chunks), BATCH):
        batch = chunks[start: start + BATCH]
        texts = [f"{c.file_path}::{c.function_name}\n{c.raw_code}" for c in batch]

        try:
            if provider == "gemini":
                embeddings = [embed_text(t) for t in texts]
            else:
                client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
                resp = client.embeddings.create(
                    model="text-embedding-3-small",
                    input=texts,
                    dimensions=SCHEMA_EMBEDDING_DIM,
                )
                embeddings = [r.embedding for r in resp.data]
        except PermissionError as e:
            raise HTTPException(status_code=401, detail=str(e))
        except (ValueError, RuntimeError) as e:
            raise HTTPException(status_code=500, detail=f"Embedding failed: {e}")

        rows = [
            {
                "file_path": batch[i].file_path,
                "function_name": batch[i].function_name,
                "raw_code": batch[i].raw_code,
                "embedding": embeddings[i],
            }
            for i in range(len(batch))
        ]
        try:
            sb.table("code_chunks").insert(rows).execute()
        except Exception as e:
            raise _classify_supabase_error(e)

        total += len(rows)

    print(f"[Bridge] Re-indexed {total} chunks for {req.file_path}", flush=True)
    return {"status": "ok", "chunks": total}


@app.post("/analyze")
def analyze(req: AnalyzeRequest):
    try:
        embedding = embed_text(req.error_log)
    except PermissionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=500, detail=f"Embedding failed: {e}")

    try:
        result = _get_supabase().rpc(
            "match_code_chunks",
            {"query_embedding": embedding, "match_count": 5},
        ).execute()
    except Exception as e:
        raise _classify_supabase_error(e)

    matches = result.data or []
    if not matches:
        return {
            "result": (
                "No code indexed yet. Click **Index Workspace** first, then try again.\n\n"
                "If you've already indexed, run `GET /health` to verify your Supabase "
                "connection and schema setup."
            )
        }

    context_blocks = "\n\n".join(
        f"**File:** `{m['file_path']}`  \n**Function:** `{m['function_name']}` "
        f"(similarity: {m['similarity']:.2f})\n```python\n{m['raw_code']}\n```"
        for m in matches
    )

    try:
        synthesis = _get_asi1().chat.completions.create(
            model="asi1",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a senior engineer triaging a production incident. "
                        "Given an error log and the most relevant code blocks from a vector search, "
                        "produce an Incident Context Kit: identify the root cause, trace the exact "
                        "dependency chain, and suggest a precise fix. "
                        "Use markdown with clear headers (## Root Cause, ## Dependency Chain, ## Fix). "
                        "Be technical and concise."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Error log:\n```\n{req.error_log}\n```\n\n"
                        f"Relevant code blocks:\n{context_blocks}"
                    ),
                },
            ],
            max_tokens=1024,
        )
        return {"result": synthesis.choices[0].message.content}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ASI1 synthesis failed: {e}")


@app.post("/fix")
def fix_code(req: FixRequest):
    try:
        embedding = embed_text(req.error_log)
    except PermissionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=500, detail=f"Embedding failed: {e}")

    try:
        result = _get_supabase().rpc(
            "match_code_chunks",
            {"query_embedding": embedding, "match_count": 5},
        ).execute()
    except Exception as e:
        raise _classify_supabase_error(e)

    matches = result.data or []
    if not matches:
        return {"edits": [], "message": "No code indexed yet. Run Index Workspace first."}

    context_blocks = "\n\n".join(
        f"File: {m['file_path']}\nFunction: {m['function_name']}\n```python\n{m['raw_code']}\n```"
        for m in matches
    )

    try:
        response = _get_asi1().chat.completions.create(
            model="asi1",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a senior engineer fixing a production bug. "
                        "Given an error log and the relevant code blocks, return ONLY a JSON array of edits. "
                        "Each edit must be an object with exactly three string fields: "
                        '"file_path" (absolute path as shown above), '
                        '"old_code" (the exact verbatim lines to replace, copied from the code block), '
                        '"new_code" (the fixed replacement). '
                        "Return raw JSON only — no markdown fences, no prose, no explanation."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Error log:\n```\n{req.error_log}\n```\n\n"
                        f"Relevant code:\n{context_blocks}"
                    ),
                },
            ],
            max_tokens=2048,
        )
        raw = response.choices[0].message.content.strip()
        raw = re.sub(r"^```[a-z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        edits = json.loads(raw)
        if not isinstance(edits, list):
            edits = [edits]
        return {"edits": edits}
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"LLM returned non-JSON: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Fix generation failed: {e}")


if __name__ == "__main__":
    print(f"[Bridge] Starting on http://0.0.0.0:{BRIDGE_PORT}", flush=True)
    print(f"[Bridge] Embedding provider: {os.getenv('EMBEDDING_PROVIDER', 'gemini')}", flush=True)
    print(f"[Bridge] Schema embedding dim: {SCHEMA_EMBEDDING_DIM}", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=BRIDGE_PORT, log_level="warning")
