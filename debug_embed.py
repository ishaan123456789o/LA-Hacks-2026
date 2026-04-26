"""Run: python debug_embed.py"""
import json, os, ssl, urllib.error, urllib.request
import certifi
from dotenv import load_dotenv

load_dotenv()

KEY   = os.environ["GEMINI_API_KEY"]
MODEL = os.getenv("GEMINI_EMBEDDING_MODEL", "models/text-embedding-004")
if not MODEL.startswith("models/"):
    MODEL = f"models/{MODEL}"

print(f"Key prefix : {KEY[:12]}...")
print(f"Model      : {MODEL}")
print()

CANDIDATES = [MODEL, "models/text-embedding-004", "models/embedding-001"]

for model in CANDIDATES:
    url  = f"https://generativelanguage.googleapis.com/v1beta/{model}:embedContent?key={KEY}"
    body = json.dumps({"model": model, "content": {"parts": [{"text": "hello"}]}}).encode()
    req  = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    ctx  = ssl.create_default_context(cafile=certifi.where())
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
            dim = len(json.loads(r.read())["embedding"]["values"])
            print(f"OK  {model}  →  {dim}-dim embedding")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="ignore")
        print(f"ERR {model}  →  HTTP {e.code}: {detail[:300]}")
    except Exception as e:
        print(f"ERR {model}  →  {e}")
