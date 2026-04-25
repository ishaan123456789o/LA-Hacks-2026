import json
import os
import sys
from dotenv import load_dotenv
from openai import OpenAI
from supabase import create_client
from uagents import Agent, Context, Protocol

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import TRACER_SEED, TRACER_PORT
from models import ParseResult

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

agent = Agent(
    name="tracer-agent",
    seed=TRACER_SEED,
    port=TRACER_PORT,
    endpoint=[f"http://127.0.0.1:{TRACER_PORT}/submit"],
    publish_agent_details=True,
)

openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

trace_protocol = Protocol("TraceProtocol")


@trace_protocol.on_message(ParseResult)
async def handle_trace(ctx: Context, sender: str, msg: ParseResult):
    blocks = json.loads(msg.blocks)
    ctx.logger.info(f"Embedding {len(blocks)} chunks for request {msg.request_id}")

    texts = [f"{b['file_path']}::{b['function_name']}\n{b['raw_code']}" for b in blocks]
    resp = openai_client.embeddings.create(model="text-embedding-3-small", input=texts)

    rows = [
        {
            "request_id": msg.request_id,
            "file_path": blocks[i]["file_path"],
            "function_name": blocks[i]["function_name"],
            "raw_code": blocks[i]["raw_code"],
            "embedding": resp.data[i].embedding,
        }
        for i in range(len(blocks))
    ]

    supabase.table("code_chunks").insert(rows).execute()
    ctx.logger.info(f"Pushed {len(rows)} embeddings to Supabase")


agent.include(trace_protocol)

if __name__ == "__main__":
    print(f"[Tracer] address: {agent.address}")
    agent.run()
