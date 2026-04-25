import os
import json
import ssl
import urllib.error
import urllib.request
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, List, Sequence

import certifi
from dotenv import load_dotenv
from openai import OpenAI
from supabase import Client, create_client

from parser_agent import CodeChunk, RepoParser


EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_GEMINI_EMBEDDING_MODELS = ("models/embedding-001", "models/text-embedding-004")
DEFAULT_BATCH_SIZE = 32
OPENAI_VECTOR_DIM = 1536
NON_OPENAI_DEFAULT_VECTOR_DIM = 768


class Agent2Tracer:
    """Agent 2: embed parsed code chunks and store them in Supabase."""

    def __init__(
        self,
        supabase_url: str,
        supabase_key: str,
        embedding_provider: str,
        openai_api_key: str | None = None,
        gemini_api_key: str | None = None,
        table_name: str = "code_nodes",
        target_vector_dim: int | None = None,
    ) -> None:
        self.supabase: Client = create_client(supabase_url, supabase_key)
        self.embedding_provider = embedding_provider.lower()
        self.openai = OpenAI(api_key=openai_api_key) if openai_api_key else None
        self.gemini_api_key = gemini_api_key
        self.table_name = table_name
        self.target_vector_dim = target_vector_dim or self._default_target_vector_dim()
        self._gemini_embedding_models_cache: List[str] | None = None

    def process_chunks(self, chunks: Sequence[CodeChunk], batch_size: int = DEFAULT_BATCH_SIZE) -> int:
        """Embed and persist all chunks. Returns inserted row count."""
        if not chunks:
            print("[Agent 2] No chunks received.")
            return 0

        total_inserted = 0
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            vectors = self._embed_batch([chunk.raw_code for chunk in batch])
            rows = [self._to_row(chunk, vector) for chunk, vector in zip(batch, vectors)]

            self.supabase.table(self.table_name).insert(rows).execute()
            total_inserted += len(rows)
            print(f"[Agent 2] Indexed {total_inserted}/{len(chunks)} chunks")

        return total_inserted

    def _embed_batch(self, texts: Sequence[str]) -> List[List[float]]:
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
            raise ValueError(
                "Unsupported EMBEDDING_PROVIDER. Use one of: openai, gemini, mock."
            )

        return [self._normalize_vector(vector) for vector in vectors]

    def _to_row(self, chunk: CodeChunk, embedding: Sequence[float]) -> dict:
        payload = asdict(chunk)
        payload["embedding"] = list(embedding)
        return payload

    def _gemini_embed(self, text: str) -> List[float]:
        if not self.gemini_api_key:
            raise ValueError("GEMINI_API_KEY is required when EMBEDDING_PROVIDER=gemini")

        configured_model = os.getenv("GEMINI_EMBEDDING_MODEL")
        candidate_models = []
        if configured_model:
            candidate_models.append(configured_model)
        candidate_models.extend(DEFAULT_GEMINI_EMBEDDING_MODELS)
        candidate_models.extend(self._discover_gemini_embedding_models())
        candidate_models = list(dict.fromkeys(candidate_models))

        last_error = None
        for model_name in candidate_models:
            try:
                return self._gemini_embed_with_model(text=text, model_name=model_name)
            except RuntimeError as err:
                last_error = err

        raise RuntimeError(
            "Gemini embedding failed for all candidate models. "
            "Verify your API key has Generative Language API access and supports embedContent, "
            "or set EMBEDDING_PROVIDER=mock to continue testing."
        ) from last_error

    def _gemini_embed_with_model(self, text: str, model_name: str) -> List[float]:
        normalized_model = (
            model_name if model_name.startswith("models/") else f"models/{model_name}"
        )
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/"
            f"{normalized_model}:embedContent?key={self.gemini_api_key}"
        )
        body = {
            "model": normalized_model,
            "content": {"parts": [{"text": text}]},
        }
        payload = self._gemini_request(url=url, body=body)

        values = payload.get("embedding", {}).get("values")
        if not values:
            raise RuntimeError(
                f"Gemini embedding response missing values for {normalized_model}: {payload}"
            )
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

        models = []
        for item in payload.get("models", []):
            supported = item.get("supportedGenerationMethods", [])
            name = item.get("name")
            if name and "embedContent" in supported:
                models.append(name)

        self._gemini_embedding_models_cache = models
        return models

    def _gemini_request(self, url: str, body: dict | None, method: str = "POST") -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        try:
            with urllib.request.urlopen(req, timeout=30, context=ssl_context) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            details = err.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Gemini request failed: {err.code} {details}") from err

    def _mock_embed(self, text: str) -> List[float]:
        # Deterministic pseudo-embedding for demo/testing without paid API quota.
        raw = text.encode("utf-8")
        if not raw:
            raw = b"\0"
        return [float(raw[i % len(raw)]) / 255.0 for i in range(128)]

    def _normalize_vector(self, vector: Sequence[float]) -> List[float]:
        values = list(vector)
        if len(values) == self.target_vector_dim:
            return values
        if len(values) > self.target_vector_dim:
            return values[: self.target_vector_dim]
        return values + [0.0] * (self.target_vector_dim - len(values))

    def _default_target_vector_dim(self) -> int:
        if self.embedding_provider == "openai":
            return OPENAI_VECTOR_DIM
        return NON_OPENAI_DEFAULT_VECTOR_DIM


def load_required_env() -> tuple[str, str, str | None, str | None]:
    load_dotenv()
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_KEY")
    provider = os.getenv("EMBEDDING_PROVIDER", "openai").lower()
    openai_api_key = os.getenv("OPENAI_API_KEY")
    gemini_api_key = os.getenv("GEMINI_API_KEY")

    missing = [
        name
        for name, value in (
            ("SUPABASE_URL", supabase_url),
            ("SUPABASE_KEY", supabase_key),
        )
        if not value
    ]
    if provider == "openai" and not openai_api_key:
        missing.append("OPENAI_API_KEY")
    if provider == "gemini" and not gemini_api_key:
        missing.append("GEMINI_API_KEY")
    if missing:
        raise ValueError(f"Missing required environment variable(s): {', '.join(missing)}")

    return supabase_url, supabase_key, openai_api_key, gemini_api_key


def index_repo(repo_path: str, skip_paths: Iterable[str] = ("venv", ".git")) -> int:
    parser = RepoParser(repo_path)
    chunks = parser.parse()

    base = Path(repo_path).resolve()
    filtered_chunks = []
    for chunk in chunks:
        path_parts = Path(chunk.file_path).parts
        if any(skip in path_parts for skip in skip_paths):
            continue
        try:
            chunk.file_path = str(Path(chunk.file_path).resolve().relative_to(base))
        except ValueError:
            chunk.file_path = str(Path(chunk.file_path).resolve())
        filtered_chunks.append(chunk)

    provider = os.getenv("EMBEDDING_PROVIDER", "openai").lower()
    configured_dim = os.getenv("TARGET_VECTOR_DIM")
    target_vector_dim = int(configured_dim) if configured_dim else None
    supabase_url, supabase_key, openai_api_key, gemini_api_key = load_required_env()
    tracer = Agent2Tracer(
        supabase_url=supabase_url,
        supabase_key=supabase_key,
        embedding_provider=provider,
        openai_api_key=openai_api_key,
        gemini_api_key=gemini_api_key,
        target_vector_dim=target_vector_dim,
    )
    return tracer.process_chunks(filtered_chunks)


if __name__ == "__main__":
    repo = os.getenv("TRACEBACK_TARGET_REPO", "./agents")
    inserted = index_repo(repo)
    print(f"[Agent 2] Completed indexing. Inserted rows: {inserted}")
