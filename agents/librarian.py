import json
import os
import ssl
import sys
import urllib.request
from datetime import datetime, timezone
from uuid import uuid4

import certifi
from dotenv import load_dotenv
from openai import OpenAI
from supabase import create_client
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement, ChatMessage, EndSessionContent, TextContent, chat_protocol_spec,
)

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import TRIAGE_SEED, TRIAGE_PORT

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

agent = Agent(
    name="triage-librarian",
    seed=TRIAGE_SEED,
    port=TRIAGE_PORT,
    endpoint=[f"http://127.0.0.1:{TRIAGE_PORT}/submit"],
    mailbox=True,
    publish_agent_details=True,
    network="testnet",
)

asi1_client = OpenAI(
    base_url="https://api.asi1.ai/v1",
    api_key=os.environ["ASI1_API_KEY"],
)
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

_openai_client = None
if os.environ.get("OPENAI_API_KEY"):
    _openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

protocol = Protocol(spec=chat_protocol_spec)


def embed_text(text: str) -> list:
    provider = os.getenv("EMBEDDING_PROVIDER", "openai").lower()
    if provider == "gemini":
        gemini_key = os.environ["GEMINI_API_KEY"]
        model = os.getenv("GEMINI_EMBEDDING_MODEL", "models/text-embedding-004")
        normalized = model if model.startswith("models/") else f"models/{model}"
        url = f"https://generativelanguage.googleapis.com/v1beta/{normalized}:embedContent?key={gemini_key}"
        body = {"model": normalized, "content": {"parts": [{"text": text}]}}
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(req, timeout=30, context=ssl_ctx) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return payload["embedding"]["values"]
    else:
        if not _openai_client:
            raise ValueError("OPENAI_API_KEY required when EMBEDDING_PROVIDER=openai")
        resp = _openai_client.embeddings.create(model="text-embedding-3-small", input=text)
        return resp.data[0].embedding


@protocol.on_message(ChatMessage)
async def handle_message(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(
        timestamp=datetime.now(), acknowledged_msg_id=msg.msg_id,
    ))

    error_log = "".join(item.text for item in msg.content if isinstance(item, TextContent))
    ctx.logger.info(f"Triaging error log ({len(error_log)} chars)")

    embedding = embed_text(error_log)
    result = supabase.rpc("match_code_chunks", {
        "query_embedding": embedding,
        "match_count": 5,
    }).execute()

    matches = result.data or []
    if matches:
        context_blocks = "\n\n".join(
            f"File: {m['file_path']}\nFunction: {m['function_name']} (similarity: {m['similarity']:.2f})\n```python\n{m['raw_code']}\n```"
            for m in matches
        )
        synthesis = asi1_client.chat.completions.create(
            model="asi1",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a senior engineer helping triage a bug. "
                        "Given an error log and the most relevant code blocks from a vector search, "
                        "identify the root cause and explain the exact dependency chain causing the issue. "
                        "Be concise and precise."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Error log:\n{error_log}\n\nRelevant code blocks:\n{context_blocks}",
                },
            ],
            max_tokens=1024,
        )
        response = synthesis.choices[0].message.content
    else:
        response = "No matching code found. Run the Parser agent on your repo first."

    await ctx.send(sender, ChatMessage(
        timestamp=datetime.now(timezone.utc),
        msg_id=uuid4(),
        content=[
            TextContent(type="text", text=response),
            EndSessionContent(type="end-session"),
        ],
    ))


@protocol.on_message(ChatAcknowledgement)
async def handle_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


agent.include(protocol, publish_manifest=True)

if __name__ == "__main__":
    print(f"[Librarian] address: {agent.address}")
    agent.run()
