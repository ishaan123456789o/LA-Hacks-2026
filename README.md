# CodeCartographer

CodeCartographer is a context retrieval engine designed to improve how large language models and autonomous coding agents interact with enterprise codebases. It uses a multi-agent architecture to parse, trace, and index repositories into a semantic graph, allowing for precise, function-level code retrieval.

## The Problem

As developers increasingly rely on AI coding agents (such as Devin, Claude, or GitHub Copilot) to navigate and modify codebases, a significant bottleneck has emerged: **context management**. 

The standard approach to giving an AI context is to load entire files, or even entire repositories, into the model's context window. This creates several critical failures:
1. **Context Window Exhaustion & Cost:** Passing thousands of lines of irrelevant boilerplate, CSS, or dependency lockfiles wastes tokens and significantly increases API costs.
2. **Behavioral Drift (Hallucinations):** When an LLM is fed a low signal-to-noise ratio, its attention mechanism struggles. It loses track of the primary objective and is more likely to hallucinate logic or generate code that violates established repository patterns.
3. **Broken Dependency Tracing:** Naive text chunking or simple `grep` searches fail to capture cross-file dependencies. If a function in `main.py` relies on a utility in `auth.py`, standard chunking often drops the necessary context.

## The Solution

CodeCartographer replaces naive file-dumping with a precise Retrieval-Augmented Generation (RAG) pipeline built specifically for code. 

Instead of reading the entire repository, the AI agent (or human developer) queries CodeCartographer with a specific task (e.g., "Retrieve the functions responsible for the user authentication flow"). The system bypasses the boilerplate and returns only the specific functional blocks and their associated dependencies required to complete the task.

## Architecture

The system operates using a multi-agent orchestration framework built on Fetch.ai's `uagents` library. 

1. **Parser Agent (Data Extraction)**
   - Ingests the target repository.
   - Uses Abstract Syntax Tree (AST) parsing to break the codebase down into logical, discrete blocks (classes, functions, and variables) rather than arbitrary text chunks.

2. **Tracer Agent (Graph Building & Embedding)**
   - Analyzes the output from the Parser to map cross-file import relationships and execution flows.
   - Passes these structural chunks through an embedding model (OpenAI `text-embedding-ada-002`).
   - Stores the embeddings and metadata in a Supabase database utilizing the `pgvector` extension.

3. **Librarian Agent (Retrieval API)**
   - Serves as the interface for the frontend or external AI agents.
   - Receives natural language queries, embeds the query, and performs a cosine similarity search against the Supabase vector database.
   - Returns the top relevant code blocks and their traced dependencies.

## Hackathon Tracks (LA Hacks)

* **Cognition:** Provides an infrastructure solution to the context bottleneck for autonomous agents, ensuring they operate with high-signal data.
* **Fetch.ai:** The core backend leverages the Agentverse platform and `uagents` framework to handle asynchronous parsing and retrieval tasks.
* **Figma:** The frontend dashboard UI was rapidly prototyped and structurally validated using Figma Make before React implementation.

## Tech Stack

* **Agent Framework:** Fetch.ai `uagents` (Python)
* **Parsing:** Python `ast` / Tree-sitter
* **Vector Database:** Supabase (`pgvector`)
* **Embeddings:** OpenAI API
* **Frontend:** React.js, TailwindCSS
* **Design:** Figma Make

## Getting Started

### Prerequisites
* Python 3.10+
* Node.js & npm
* Supabase Account
* OpenAI API Key

### 1. Installation
```bash
git clone [https://github.com/yourusername/CodeCartographer.git](https://github.com/yourusername/CodeCartographer.git)
cd CodeCartographer

# Install Python backend dependencies
cd backend
pip install -r requirements.txt

# Install React frontend dependencies
cd ../frontend
npm install
