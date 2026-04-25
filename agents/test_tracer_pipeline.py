import os

from dotenv import load_dotenv
from supabase import create_client

from tracer_agent import index_repo


def run_test() -> None:
    load_dotenv()
    target_repo = os.getenv("TRACEBACK_TARGET_REPO", "./agents")

    inserted = index_repo(target_repo)
    print(f"[Test] Agent 2 inserted {inserted} rows.")

    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_KEY")
    client = create_client(supabase_url, supabase_key)

    result = client.table("code_nodes").select("id", count="exact").limit(1).execute()
    print(f"[Test] code_nodes total rows: {result.count}")


if __name__ == "__main__":
    run_test()
