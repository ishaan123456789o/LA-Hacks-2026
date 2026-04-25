create extension if not exists vector;

create table if not exists code_nodes (
  id bigserial primary key,
  file_path text not null,
  function_name text not null,
  raw_code text not null,
  embedding vector(1536) not null,
  created_at timestamptz not null default now()
);

create or replace function match_code_nodes(
  query_embedding vector(1536),
  match_count int default 3
)
returns table (
  id uuid,
  file_path text,
  function_name text,
  raw_code text,
  similarity float
)
language sql
stable
as $$
  select
    code_nodes.id,
    code_nodes.file_path,
    code_nodes.function_name,
    code_nodes.raw_code,
    1 - (code_nodes.embedding <=> query_embedding) as similarity
  from code_nodes
  order by code_nodes.embedding <=> query_embedding
  limit match_count;
$$;
