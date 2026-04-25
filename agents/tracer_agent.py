import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Iterable, List, Sequence

import certifi
from dotenv import load_dotenv
from openai import OpenAI
from supabase import Client, create_client
from uagents import Agent, Context, Protocol

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import TRACER_SEED, TRACER_PORT
from models import ParseResult

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_GEMINI_EMBEDDING_MODELS = ("models/embedding-001", "models/text-embedding-004")
DEFAULT_BATCH_SIZE = 32
OPENAI_VECTOR_DIM = 1536
NON_OPENAI_DEFAULT_VECTOR_DIM = 768


@dataclass
class CodeChunk:
    file_path: str
    function_name: str
    raw_code: str


class Agent2Tracer:
    """Embed code chunks and store them in Supabase. Supports OpenAI, Gemini, and mock."""

    def __init__(
        self,
        supabase_url: str,
        supabase_key: str,
        embedding_provider: str,
        openai_api_key: str | None = None,
        gemini_api_key: str | None = None,
        table_name: str = "code_chunks",
        target_vector_dim: int | None = None,
    ) -> None:
        self.supabase: Client = create_client(supabase_url, supabase_key)
        self.embedding_provider = embedding_provider.lower()
        self.openai = OpenAI(api_key=openai_api_key) if openai_api_key else None
        self.gemini_api_key = gemini_api_key
        self.table_name = table_name
        self.target_vector_dim = target_vector_dim or self._default_target_vector_dim()
        self._gemini_embedding_models_cache: List[str] | None = None

    def embed_batch(self, texts: Sequence[str]) -> List[List[float]]:
        if self.embedding_provider == "openai":
            if not self.openai:
                raise ValueError("OPENAI_API_KEY is required when EMBEDDING_PROVIDER=openai")
            response = self.openai.embeddings.create(model=EMBEDDING_MODEL, input=list(texts))
            vectors = [item.embedding for item in response.data]
        elif self.embedding_provider == "gemini":
            vectors = [self._gemini_embed(text) for text in texts]
        elif self.embedding_provider == "mock":
            vectors = [self._mock_embed(text) for text in texts]
        else:
            raise ValueError("Unsupported EMBEDDING_PROVIDER. Use one of: openai, gemini, mock.")
        return [self._normalize_vector(v) for v in vectors]

    def process_chunks(self, chunks: Sequence[CodeChunk], request_id: str = "", batch_size: int = DEFAULT_BATCH_SIZE) -> int:
        if not chunks:
            return 0
        total_inserted = 0
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            embeddings = self.embed_batch([c.raw_code for c in batch])
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
            self.supabase.table(self.table_name).insert(rows).execute()
            total_inserted += len(rows)
            print(f"[Tracer] Indexed {total_inserted}/{len(chunks)} chunks")
        return total_inserted

    def _gemini_embed(self, text: str) -> List[float]:
        if not self.gemini_api_key:
            raise ValueError("GEMINI_API_KEY is required when EMBEDDING_PROVIDER=gemini")
        configured_model = os.getenv("GEMINI_EMBEDDING_MODEL")
        candidates = []
        if configured_model:
            candidates.append(configured_model)
        candidates.extend(DEFAULT_GEMINI_EMBEDDING_MODELS)
        candidates.extend(self._discover_gemini_embedding_models())
        candidates = list(dict.fromkeys(candidates))
        last_error = None
        for model_name in candidates:
            try:
                return self._gemini_embed_with_model(text=text, model_name=model_name)
            except RuntimeError as err:
                last_error = err
        raise RuntimeError("Gemini embedding failed for all candidate models.") from last_error

    def _gemini_embed_with_model(self, text: str, model_name: str) -> List[float]:
        normalized = model_name if model_name.startswith("models/") else f"models/{model_name}"
        url = f"https://generativelanguage.googleapis.com/v1beta/{normalized}:embedContent?key={self.gemini_api_key}"
        body = {"model": normalized, "content": {"parts": [{"text": text}]}}
        payload = self._gemini_request(url=url, body=body)
        values = payload.get("embedding", {}).get("values")
        if not values:
            raise RuntimeError(f"Gemini embedding response missing values for {normalized}: {payload}")
        return values

    def _discover_gemini_embedding_models(self) -> List[str]:
        if self._gemini_embedding_models_cache is not None:
            return self._gemini_embedding_models_cache
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={self.gemini_api_key}"
        try:
            payload = self._gemini_request(url=url, body=None, method="GET")
        except RuntimeError:
            self._gemini_embedding_models_cache = []
            return self._gemini_embedding_models_cache
        models = [
            item["name"]
            for item in payload.get("models", [])
            if item.get("name") and "embedContent" in item.get("supportedGenerationMethods", [])
        ]
        self._gemini_embedding_models_cache = models
        return models

    def _gemini_request(self, url: str, body: dict | None, method: str = "POST") -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method=method)
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        try:
            with urllib.request.urlopen(req, timeout=30, context=ssl_context) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            details = err.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Gemini request failed: {err.code} {details}") from err

    def _mock_embed(self, text: str) -> List[float]:
        raw = text.encode("utf-8") or b"\0"
        return [float(raw[i % len(raw)]) / 255.0 for i in range(128)]

    def _normalize_vector(self, vector: Sequence[float]) -> List[float]:
        values = list(vector)
        if len(values) == self.target_vector_dim:
            return values
        if len(values) > self.target_vector_dim:
            return values[: self.target_vector_dim]
        return values + [0.0] * (self.target_vector_dim - len(values))

    def _default_target_vector_dim(self) -> int:
        return OPENAI_VECTOR_DIM if self.embedding_provider == "openai" else NON_OPENAI_DEFAULT_VECTOR_DIM


# uAgents wrapper
agent = Agent(
    name="tracer-agent",
    seed=TRACER_SEED,
    port=TRACER_PORT,
    endpoint=[f"http://127.0.0.1:{TRACER_PORT}/submit"],
    mailbox=True,
    publish_agent_details=True,
    network="testnet",
)

trace_protocol = Protocol("TraceProtocol")


@trace_protocol.on_message(ParseResult)
async def handle_trace(ctx: Context, sender: str, msg: ParseResult):
    blocks = json.loads(msg.blocks)
    ctx.logger.info(f"Embedding {len(blocks)} chunks for request {msg.request_id}")

    chunks = [CodeChunk(**b) for b in blocks]
    provider = os.getenv("EMBEDDING_PROVIDER", "openai").lower()
    tracer = Agent2Tracer(
        supabase_url=os.environ["SUPABASE_URL"],
        supabase_key=os.environ["SUPABASE_KEY"],
        embedding_provider=provider,
        openai_api_key=os.environ.get("OPENAI_API_KEY"),
        gemini_api_key=os.environ.get("GEMINI_API_KEY"),
    )
    inserted = tracer.process_chunks(chunks, request_id=msg.request_id)
    ctx.logger.info(f"Pushed {inserted} embeddings to Supabase")


agent.include(trace_protocol)

if __name__ == "__main__":
    print(f"[Tracer] address: {agent.address}")
    agent.run()
