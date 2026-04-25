# TraceBack

TraceBack is a context retrieval engine designed to accelerate incident response and bug triage. Built as a native VS Code extension, it utilizes a multi-agent orchestration framework to parse, trace, and index repositories into a semantic graph, allowing developers to instantly map raw error logs to the specific functions causing the crash.

## The Problem

When managing Agile backlogs and triaging production issues at scale, the primary bottleneck is rarely writing the fix—it is the hours spent context-switching and hunting for the broken dependency. 

Furthermore, as developers increasingly rely on AI coding agents (such as Devin or GitHub Copilot) to resolve these bugs, a critical failure point emerges: **context exhaustion**. Dumping an entire repository or raw stack trace into an LLM's context window leads to:
1. **Behavioral Drift (Hallucinations):** The AI loses focus amid boilerplate code and suggests fixes that violate repository patterns.
2. **Token Waste:** Processing irrelevant dependencies is slow and expensive.
3. **Broken Dependency Tracing:** Naive RAG implementations fail to capture complex, cross-file import relationships.

## The Solution

TraceBack serves as an automated Incident Context Engine. Instead of treating debugging as a manual search or a brute-force AI prompt, TraceBack acts as a precision middleware.

A developer pastes a raw error log into the VS Code sidebar. Behind the scenes, a swarm of agents processes the stack trace against a pre-indexed vector map of the repository. TraceBack instantly returns an "Incident Context Kit"—the exact chain of files and functional blocks responsible for the crash. This hands the developer (or an autonomous AI agent) the high-signal, zero-noise context required to write the fix immediately.

## Architecture & Agent Swarm

The system operates using a decentralized, multi-agent framework built on Fetch.ai's `uagents` library, communicating with a React-based VS Code Webview.

1. **Parser Agent (Data Extraction)**
   - Ingests the target repository.
   - Utilizes Abstract Syntax Tree (AST) parsing to break the codebase down into discrete logical blocks (classes, functions, and variables).

2. **Tracer Agent (Graph Building & Embedding)**
   - Analyzes the parser's output to map cross-file dependencies and execution flows.
   - Embeds these structural chunks using an embedding model and stores them in a Supabase database utilizing the `pgvector` extension.

3. **Triage Librarian Agent (Retrieval API)**
   - Interfaces directly with the VS Code extension.
   - Receives raw error logs, extracts failing function names/files, and performs a cosine similarity search against the Supabase vector database.
   - Returns the verified dependency chain back to the IDE sidebar.

## Hackathon Tracks (LA Hacks)

* **Cognition:** Acts as a pre-processing infrastructure layer for autonomous AI agents, ensuring they receive verified, hyper-specific context before attempting bug resolution.
* **Fetch.ai:** The core backend leverages the Agentverse platform and `uagents` framework to distribute parsing, embedding, and retrieval across distinct micro-agents.
* **Figma Make:** The extension's sidebar layout, including the error input and code block displays, was rapidly prototyped and structurally validated using Figma Make before React implementation.

## Tech Stack

* **Extension Host:** VS Code Extension API
* **Frontend:** React.js / TailwindCSS (rendered via VS Code Webview)
* **Agent Framework:** Fetch.ai `uagents` (Python)
* **Parsing:** Python `ast`
* **Vector Database:** Supabase (`pgvector`)
* **Design Prototyping:** Figma Make

## Getting Started

### Prerequisites
* Node.js & npm
* Python 3.10+
* Supabase Account
* VS Code Extension Manager (`vsce`)

### 1. Installation
```bash
git clone [https://github.com/yourusername/TraceBack.git](https://github.com/yourusername/TraceBack.git)
cd TraceBack

# Install VS Code Extension & Webview dependencies
cd extension
npm install

# Install Python backend dependencies
cd ../backend
pip install -r requirements.txt