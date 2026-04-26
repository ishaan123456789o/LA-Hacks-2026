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
from uuid import uuid4

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
        resp = client.embeddings.create(
            model="text-embedding-3-small",
            input=text,
            dimensions=SCHEMA_EMBEDDING_DIM,
        )
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
    except Exception:
        return chunks

    try:
        tree = ast.parse(src)
        lines = src.splitlines()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                raw = "\n".join(lines[node.lineno - 1: node.end_lineno])
                chunks.append(_Chunk(file_path, node.name, raw))
        # Script-style files with no top-level defs → use whole file as one chunk
        if not chunks:
            chunks.append(_Chunk(file_path, "<module>", src))
    except SyntaxError:
        # Keep triage functional for files that currently fail to parse.
        chunks.append(_Chunk(file_path, "<module_syntax_error>", src))
    except Exception:
        return chunks
    return chunks


def _parse_any_file(file_path: str) -> List[_Chunk]:
    """
    Parse Python files structurally; treat any other text-like file as line chunks.
    This keeps fix generation usable across JS/TS/JSON/YAML/etc.
    """
    if file_path.endswith(".py"):
        return _parse_file(file_path)

    try:
        src = open(file_path, encoding="utf-8", errors="ignore").read()
    except Exception:
        return []
    if not src.strip():
        return []

    lines = src.splitlines()
    window = 120
    chunks: List[_Chunk] = []
    for i in range(0, max(1, len(lines)), window):
        block = "\n".join(lines[i: i + window]) if lines else src
        if not block.strip():
            continue
        chunks.append(_Chunk(file_path, f"<chunk_{(i // window) + 1}>", block))
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


class FixCleanupRequest(BaseModel):
    request_id: str


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
    matches = _retrieve_relevant_chunks(req.error_log, match_count=8)
    print(f"[Bridge] Analyze: {len(matches)} candidate chunks", flush=True)

    if not matches:
        return {
            "result": (
                "Could not find any readable Python files in the traceback. "
                "Make sure the file paths in the error are accessible on this machine."
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
                        "Use plain text with clear section titles (Root Cause, Dependency Chain, Fix). "
                        "Do not use markdown formatting, bold text, or code blocks. Be technical and concise."
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
        return {"result": synthesis.choices[0].message.content or ""}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ASI1 synthesis failed: {e}")


def _extract_traceback_files(error_log: str) -> List[str]:
    """Return unique file paths mentioned in traceback/log formats."""
    seen: dict = {}
    patterns = [
        r'File "([^"]+)"',                      # Python traceback
        r'at ((?:/[^:\n]+)+):\d+(?::\d+)?',     # JS/TS stacktrace absolute paths
        r'((?:/[^:\n]+)+\.[A-Za-z0-9_]+):\d+',  # Generic /path/file.ext:line
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, error_log):
            fp = m.group(1).strip()
            if fp in seen:
                continue
            seen[fp] = True
    return list(seen)


def _extract_failure_signals(error_log: str) -> Dict[str, List[str]]:
    """
    Parse failing files/functions from raw logs with an LLM-first strategy.
    Falls back to regex extraction so the pipeline still works if the LLM fails.
    """
    fallback_files = _extract_traceback_files(error_log)
    fallback_functions = re.findall(r"in ([A-Za-z_][A-Za-z0-9_]*)", error_log)

    try:
        response = _get_asi1().chat.completions.create(
            model="asi1",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Extract failing Python symbols from an error log. "
                        "Extract failing symbols and file paths from an error log. "
                        "Return JSON only with this exact schema: "
                        '{"files": ["..."], "functions": ["..."]}. '
                        "Include only real values found in the log. No markdown."
                    ),
                },
                {"role": "user", "content": error_log},
            ],
            max_tokens=250,
        )
        raw = response.choices[0].message.content.strip()
        raw = re.sub(r"^```[a-z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        parsed = json.loads(raw)
        files = [f for f in parsed.get("files", []) if isinstance(f, str)]
        functions = [f for f in parsed.get("functions", []) if isinstance(f, str)]
        return {
            "files": list(dict.fromkeys(files + fallback_files)),
            "functions": list(dict.fromkeys(functions + fallback_functions)),
        }
    except Exception:
        return {
            "files": list(dict.fromkeys(fallback_files)),
            "functions": list(dict.fromkeys(fallback_functions)),
        }


def _retrieve_relevant_chunks(error_log: str, match_count: int = 8) -> List[Dict[str, Any]]:
    """
    Embed extracted failure signals + raw log, then query Supabase pgvector RPC.
    Returns rows shaped for the webview markdown renderer.
    """
    signals = _extract_failure_signals(error_log)
    query_text = "\n".join(
        [
            f"error_log:\n{error_log}",
            f"failing_files: {', '.join(signals['files'])}",
            f"failing_functions: {', '.join(signals['functions'])}",
        ]
    )
    fallback: List[Dict[str, Any]] = []
    for fp in signals["files"]:
        for chunk in _parse_file(fp):
            fallback.append(
                {
                    "file_path": chunk.file_path,
                    "function_name": chunk.function_name,
                    "raw_code": chunk.raw_code,
                    "similarity": 1.0,
                }
            )

    matches: List[Dict[str, Any]] = []
    try:
        embedding = embed_text(query_text)
        result = _get_supabase().rpc(
            "match_code_chunks",
            {"query_embedding": embedding, "match_count": match_count},
        ).execute()
        matches = result.data or []
    except Exception as e:
        # Keep incident triage usable even if vector infra is temporarily unavailable.
        print(f"[Bridge] Vector retrieval unavailable, using traceback fallback: {e}", flush=True)
        matches = []

    if matches and fallback:
        # Prioritize exact traceback files, then enrich with semantic vector hits.
        merged = fallback + matches
        deduped: List[Dict[str, Any]] = []
        seen = set()
        for m in merged:
            key = (m.get("file_path", ""), m.get("function_name", ""), m.get("raw_code", ""))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(m)
        return deduped

    if matches:
        return matches

    if fallback:
        return fallback

    # Last-resort fallback: keep analysis functional even when file paths are
    # inaccessible or unparsable by providing the raw incident payload as context.
    function_hint = signals["functions"][0] if signals["functions"] else "<unknown>"
    return [
        {
            "file_path": signals["files"][0] if signals["files"] else "<unresolved_path>",
            "function_name": function_hint,
            "raw_code": (
                "# Raw incident log (file path could not be read on this machine)\n"
                f"{error_log}"
            ),
            "similarity": 0.0,
        }
    ]


def _stage_fix_chunks(file_paths: List[str], request_id: str) -> int:
    """
    Temporarily embed and insert chunks for files likely involved in the fix.
    These staged chunks are cleaned up after apply via /fix-cleanup.
    """
    all_chunks: List[_Chunk] = []
    for file_path in file_paths:
        all_chunks.extend(_parse_any_file(file_path))

    if not all_chunks:
        return 0

    sb = _get_supabase()
    total = 0
    batch_size = 20
    for start in range(0, len(all_chunks), batch_size):
        batch = all_chunks[start: start + batch_size]
        texts = [f"{c.file_path}::{c.function_name}\n{c.raw_code}" for c in batch]
        embeddings = [embed_text(t) for t in texts]
        rows = [
            {
                "request_id": request_id,
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
    return total


def _is_edit_candidate_valid(file_path: str, old_code: str, new_code: str) -> bool:
    """
    Lightweight guardrails so we only return plausible edits.
    """
    if not new_code or not new_code.strip():
        return False
    if new_code.strip() == old_code.strip():
        return False
    if "```" in new_code:
        return False
    # Reject obviously destructive rewrites for function-level edits.
    if old_code.strip() and len(new_code) > max(20000, len(old_code) * 5):
        return False
    if file_path.endswith(".py"):
        try:
            ast.parse(new_code)
        except Exception:
            return False
    return True


@app.post("/fix")
def fix_code(req: FixRequest):
    fix_request_id = f"fix:{uuid4()}"
    signals = _extract_failure_signals(req.error_log)
    staged_chunks = _stage_fix_chunks(signals["files"], fix_request_id)
    print(f"[Bridge] Fix staging: {staged_chunks} chunks ({fix_request_id})", flush=True)

    matches = _retrieve_relevant_chunks(req.error_log, match_count=8)
    print(f"[Bridge] Fix: {len(matches)} candidate chunks", flush=True)

    if not matches:
        return {
            "edits": [],
            "fix_request_id": fix_request_id,
            "message": "Could not find any readable files in the traceback.",
        }

    # Number each block so the LLM identifies it by index — no fragile string matching.
    context_blocks = "\n\n".join(
        f"[{i}] File: {m['file_path']}\nFunction: {m['function_name']}\n```python\n{m['raw_code']}\n```"
        for i, m in enumerate(matches)
    )

    try:
        response = _get_asi1().chat.completions.create(
            model="asi1",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a senior engineer fixing a production bug. "
                        "Given an error log and numbered code blocks, return ONLY a JSON array of edits. "
                        "Each edit must be an object with exactly two fields: "
                        '"block_index" (integer — the [N] number of the block to fix), '
                        '"new_code" (the complete fixed replacement for that function, preserving indentation exactly). '
                        "Return raw JSON only — no markdown fences, no prose, no explanation."
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
            max_tokens=2048,
        )
        raw = response.choices[0].message.content.strip()
        raw = re.sub(r"^```[a-z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        edits = json.loads(raw)
        if not isinstance(edits, list):
            edits = [edits]

        full_edits = []
        for edit in edits:
            idx = edit.get("block_index")
            if idx is None:
                idx = edit.get("index", edit.get("chunk_index"))
            if isinstance(idx, str) and idx.isdigit():
                idx = int(idx)
            new_code = edit.get("new_code", "")
            if idx is None or not new_code:
                print(f"[Bridge] Fix: malformed edit {edit!r}", flush=True)
                continue
            if not isinstance(idx, int) or not (0 <= idx < len(matches)):
                print(f"[Bridge] Fix: block_index {idx!r} out of range", flush=True)
                continue
            m = matches[idx]
            if not _is_edit_candidate_valid(m["file_path"], m["raw_code"], new_code):
                print(f"[Bridge] Fix: rejected invalid edit for {m['file_path']}", flush=True)
                continue
            full_edits.append({
                "file_path": m["file_path"],
                "old_code": m["raw_code"],
                "new_code": new_code,
            })

        if not full_edits:
            # Recovery path for syntax-error incidents where the model may omit block_index.
            first = matches[0]
            fallback_response = _get_asi1().chat.completions.create(
                model="asi1",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Return ONLY the complete fixed replacement code for the provided Python block. "
                            "Do not return JSON, markdown, or explanation."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Error log:\n{req.error_log}\n\n"
                            f"Block to fix:\n```python\n{first['raw_code']}\n```"
                        ),
                    },
                ],
                max_tokens=2048,
            )
            raw_code = fallback_response.choices[0].message.content.strip()
            raw_code = re.sub(r"^```[a-z]*\s*", "", raw_code)
            raw_code = re.sub(r"\s*```$", "", raw_code)
            if raw_code and _is_edit_candidate_valid(first["file_path"], first["raw_code"], raw_code):
                full_edits.append(
                    {
                        "file_path": first["file_path"],
                        "old_code": first["raw_code"],
                        "new_code": raw_code,
                    }
                )

        return {"edits": full_edits, "fix_request_id": fix_request_id}
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"LLM returned non-JSON: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Fix generation failed: {e}")


@app.post("/fix-cleanup")
def cleanup_fix_chunks(req: FixCleanupRequest):
    if not req.request_id.startswith("fix:"):
        raise HTTPException(status_code=400, detail="Invalid fix request id")
    try:
        _get_supabase().table("code_chunks").delete().eq("request_id", req.request_id).execute()
        return {"ok": True, "request_id": req.request_id}
    except Exception as e:
        raise _classify_supabase_error(e)


if __name__ == "__main__":
    print(f"[Bridge] Starting on http://0.0.0.0:{BRIDGE_PORT}", flush=True)
    print(f"[Bridge] Embedding provider: {os.getenv('EMBEDDING_PROVIDER', 'gemini')}", flush=True)
    print(f"[Bridge] Schema embedding dim: {SCHEMA_EMBEDDING_DIM}", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=BRIDGE_PORT, log_level="warning")
