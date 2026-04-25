import ast
import json
import os
import sys
from dataclasses import dataclass, asdict
from typing import List
from dotenv import load_dotenv
from uagents import Agent, Context, Protocol
from uagents.crypto import Identity

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import PARSER_SEED, PARSER_PORT, TRACER_SEED
from models import ParseRequest, ParseResult

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

TRACER_ADDRESS = Identity.from_seed(TRACER_SEED, 0).address


# =========================
# AST Parser Core (existing)
# =========================
@dataclass
class CodeChunk:
    file_path: str
    function_name: str
    raw_code: str


class RepoParser:
    def __init__(self, repo_path: str):
        self.repo_path = repo_path
        self.chunks: List[CodeChunk] = []

    def parse(self) -> List[CodeChunk]:
        for root, _, files in os.walk(self.repo_path):
            for file in files:
                if file.endswith(".py"):
                    self._parse_file(os.path.join(root, file))
        return self.chunks

    def _parse_file(self, file_path: str):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                source = f.read()
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    chunk = self._extract_chunk(node, source, file_path)
                    if chunk:
                        self.chunks.append(chunk)
        except Exception as e:
            print(f"[Parser Error] {file_path}: {e}")

    def _extract_chunk(self, node, source: str, file_path: str):
        try:
            lines = source.splitlines()
            raw_code = "\n".join(lines[node.lineno - 1 : node.end_lineno])
            return CodeChunk(file_path=file_path, function_name=node.name, raw_code=raw_code)
        except Exception as e:
            print(f"[Chunk Error] {file_path}: {e}")
            return None


# =========================
# uAgents Wrapper
# =========================
agent = Agent(
    name="parser-agent",
    seed=PARSER_SEED,
    port=PARSER_PORT,
    endpoint=[f"http://127.0.0.1:{PARSER_PORT}/submit"],
    mailbox=True,
    publish_agent_details=True,
    network="testnet",
)

parse_protocol = Protocol("ParseProtocol")


@parse_protocol.on_message(ParseRequest)
async def handle_parse(ctx: Context, sender: str, msg: ParseRequest):
    ctx.logger.info(f"Parsing repo: {msg.repo_path}")
    chunks = RepoParser(msg.repo_path).parse()
    ctx.logger.info(f"Extracted {len(chunks)} chunks — forwarding to tracer")
    await ctx.send(TRACER_ADDRESS, ParseResult(
        request_id=msg.request_id,
        blocks=json.dumps([asdict(c) for c in chunks]),
    ))


agent.include(parse_protocol)

if __name__ == "__main__":
    print(f"[Parser] address: {agent.address}")
    agent.run()
