import ast
import os
from dataclasses import dataclass, asdict
from typing import List


# =========================
# Data मॉडल (Protocol Match)
# =========================
@dataclass
class CodeChunk:
    file_path: str
    function_name: str
    raw_code: str


# =========================
# AST Parser Core
# =========================
class RepoParser:
    def __init__(self, repo_path: str):
        self.repo_path = repo_path
        self.chunks: List[CodeChunk] = []

    def parse(self) -> List[CodeChunk]:
        """Walk the repo and parse all Python files."""
        for root, _, files in os.walk(self.repo_path):
            for file in files:
                if file.endswith(".py"):
                    full_path = os.path.join(root, file)
                    self._parse_file(full_path)

        return self.chunks

    def _parse_file(self, file_path: str):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                source = f.read()

            tree = ast.parse(source)

            # Attach source to tree for slicing
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    chunk = self._extract_chunk(node, source, file_path)
                    if chunk:
                        self.chunks.append(chunk)

        except Exception as e:
            print(f"[Parser Error] {file_path}: {e}")

    def _extract_chunk(self, node, source: str, file_path: str):
        """Extract exact source code for a node."""
        try:
            # Python 3.8+ has end_lineno
            start = node.lineno - 1
            end = node.end_lineno

            lines = source.splitlines()
            raw_code = "\n".join(lines[start:end])

            return CodeChunk(
                file_path=file_path,
                function_name=node.name,
                raw_code=raw_code
            )
        except Exception as e:
            print(f"[Chunk Error] {file_path}: {e}")
            return None


# =========================
# Agent 1 Wrapper (Stub for uAgents)
# =========================
class Agent1Parser:
    def __init__(self, repo_path: str):
        self.repo_path = repo_path

    def run(self):
        parser = RepoParser(self.repo_path)
        chunks = parser.parse()

        print(f"[Agent 1] Extracted {len(chunks)} code chunks")

        # 🔗 This is where you'd send to Agent 2
        self.send_to_agent2(chunks)

    def send_to_agent2(self, chunks: List[CodeChunk]):
        """Stub for uAgents communication"""
        for chunk in chunks:
            payload = asdict(chunk)

            # Replace this with actual uAgents send()
            print("\n--- Sending Chunk ---")
            print(payload)


# =========================
# Run Script
# =========================
if __name__ == "__main__":
    repo_path = "./dummy_repo"  # change this
    agent = Agent1Parser(repo_path)
    agent.run()