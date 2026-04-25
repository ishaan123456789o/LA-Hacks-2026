"""
Bridge server — FastAPI on port 8080.
Spawns all three uagents on startup, serves VS Code extension REST API.
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
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import List

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
load_dotenv(ROOT_DIR / ".env")

BRIDGE_PORT = 8080

asi1_client = OpenAI(
    base_url="https://api.asi1.ai/v1",
    api_key=os.environ["ASI1_API_KEY"],
)
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

app = FastAPI(title="TraceBack Bridge")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Embedding ──────────────────────────────────────────────────────────────

def embed_text(text: str) -> list:
    provider = os.getenv("EMBEDDING_PROVIDER", "openai").lower()
    if provider == "gemini":
        key = os.environ["GEMINI_API_KEY"]
        model = os.getenv("GEMINI_EMBEDDING_MODEL", "models/text-embedding-004")
        if not model.startswith("models/"):
            model = f"models/{model}"
        url = f"https://generativelanguage.googleapis.com/v1beta/{model}:embedContent?key={key}"
        body = {"model": model, "content": {"parts": [{"text": text}]}}
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
        )
        ctx = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            return json.loads(resp.read())["embedding"]["values"]
    else:
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        resp = client.embeddings.create(model="text-embedding-3-small", input=text)
        return resp.data[0].embedding


# ── AST repo parser ────────────────────────────────────────────────────────

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


# ── Agent subprocess launcher ──────────────────────────────────────────────

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


# ── API models ─────────────────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    error_log: str


class IndexRequest(BaseModel):
    repo_path: str


class ReindexFileRequest(BaseModel):
    file_path: str


class FixRequest(BaseModel):
    error_log: str


# ── Routes ─────────────────────────────────────────────────────────────────

@app.get("/status")
def status():
    try:
        result = supabase.table("code_chunks").select("id", count="exact").limit(1).execute()
        return {"indexed_chunks": result.count or 0, "ok": True}
    except Exception as e:
        return {"indexed_chunks": 0, "ok": False, "error": str(e)}


@app.post("/index")
def index_repo(req: IndexRequest):
    chunks = _parse_repo(req.repo_path)
    if not chunks:
        return {"status": "ok", "chunks": 0}

    # Remove stale chunks before re-indexing to avoid duplicates
    try:
        supabase.table("code_chunks").delete().like("file_path", f"{req.repo_path}%").execute()
    except Exception:
        pass

    BATCH = 20
    total = 0
    provider = os.getenv("EMBEDDING_PROVIDER", "openai").lower()

    for start in range(0, len(chunks), BATCH):
        batch = chunks[start: start + BATCH]
        texts = [f"{c.file_path}::{c.function_name}\n{c.raw_code}" for c in batch]

        if provider == "gemini":
            embeddings = [embed_text(t) for t in texts]
        else:
            client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
            resp = client.embeddings.create(model="text-embedding-3-small", input=texts)
            embeddings = [r.embedding for r in resp.data]

        rows = [
            {
                "file_path": batch[i].file_path,
                "function_name": batch[i].function_name,
                "raw_code": batch[i].raw_code,
                "embedding": embeddings[i],
            }
            for i in range(len(batch))
        ]
        supabase.table("code_chunks").insert(rows).execute()
        total += len(rows)
        print(f"[Bridge] Indexed {total}/{len(chunks)} chunks", flush=True)

    return {"status": "ok", "chunks": total}


@app.post("/reindex-file")
def reindex_file(req: ReindexFileRequest):
    # Remove stale embeddings for this file (handles deletes too — if file is gone, parse returns [])
    try:
        supabase.table("code_chunks").delete().eq("file_path", req.file_path).execute()
    except Exception:
        pass

    chunks = _parse_file(req.file_path)
    if not chunks:
        return {"status": "ok", "chunks": 0}

    provider = os.getenv("EMBEDDING_PROVIDER", "openai").lower()
    BATCH = 20
    total = 0
    for start in range(0, len(chunks), BATCH):
        batch = chunks[start: start + BATCH]
        texts = [f"{c.file_path}::{c.function_name}\n{c.raw_code}" for c in batch]
        if provider == "gemini":
            embeddings = [embed_text(t) for t in texts]
        else:
            client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
            resp = client.embeddings.create(model="text-embedding-3-small", input=texts)
            embeddings = [r.embedding for r in resp.data]
        rows = [
            {
                "file_path": batch[i].file_path,
                "function_name": batch[i].function_name,
                "raw_code": batch[i].raw_code,
                "embedding": embeddings[i],
            }
            for i in range(len(batch))
        ]
        supabase.table("code_chunks").insert(rows).execute()
        total += len(rows)

    print(f"[Bridge] Re-indexed {total} chunks for {req.file_path}", flush=True)
    return {"status": "ok", "chunks": total}


@app.post("/analyze")
def analyze(req: AnalyzeRequest):
    try:
        embedding = embed_text(req.error_log)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Embedding failed: {e}")

    result = supabase.rpc("match_code_chunks", {
        "query_embedding": embedding,
        "match_count": 5,
    }).execute()

    matches = result.data or []
    if not matches:
        return {"result": "No code indexed yet. Click **Index Workspace** first, then try again."}

    context_blocks = "\n\n".join(
        f"**File:** `{m['file_path']}`  \n**Function:** `{m['function_name']}` "
        f"(similarity: {m['similarity']:.2f})\n```python\n{m['raw_code']}\n```"
        for m in matches
    )

    try:
        synthesis = asi1_client.chat.completions.create(
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
                    "content": f"Error log:\n```\n{req.error_log}\n```\n\nRelevant code blocks:\n{context_blocks}",
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
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Embedding failed: {e}")

    result = supabase.rpc("match_code_chunks", {
        "query_embedding": embedding,
        "match_count": 5,
    }).execute()

    matches = result.data or []
    if not matches:
        return {"edits": [], "message": "No code indexed yet."}

    context_blocks = "\n\n".join(
        f"File: {m['file_path']}\nFunction: {m['function_name']}\n```python\n{m['raw_code']}\n```"
        for m in matches
    )

    try:
        response = asi1_client.chat.completions.create(
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
        # Strip accidental markdown code fences
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
    print(f"[Bridge] Starting on http://127.0.0.1:{BRIDGE_PORT}", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=BRIDGE_PORT, log_level="warning")
