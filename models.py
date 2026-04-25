from uagents import Model

class ParseRequest(Model):
    repo_path: str
    request_id: str

class ParseResult(Model):
    request_id: str
    blocks: str  # JSON-encoded list[dict]
