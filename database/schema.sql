-- TraceBack canonical Supabase schema
--
-- CANONICAL EMBEDDING DIMENSION: 768
--   Gemini  → GEMINI_EMBEDDING_MODEL=models/text-embedding-004  (native 768-dim)
--             Experimental 3072-dim models are auto-truncated via outputDimensionality=768.
--   OpenAI  → text-embedding-3-small with dimensions=768

create extension if not exists vector;

create table if not exists code_chunks (
  id            bigserial primary key,
  request_id    text,
  file_path     text,
  function_name text,
  raw_code      text,
  embedding     vector(768)   -- must match SCHEMA_EMBEDDING_DIM in agents/bridge.py
);

create index if not exists code_chunks_embedding_idx
  on code_chunks using ivfflat (embedding vector_cosine_ops)
  with (lists = 100);

create or replace function match_code_chunks(
  query_embedding vector(768),   -- must match table column dimension
  match_count     int default 5
)
returns table (
  id            bigint,
  file_path     text,
  function_name text,
  raw_code      text,
  similarity    float
)
language sql stable as $$
  select id, file_path, function_name, raw_code,
    1 - (embedding <=> query_embedding) as similarity
  from code_chunks
  order by embedding <=> query_embedding
  limit match_count;
$$;
