"""
Test file for TraceBack extension.
Run with: python test_buggy.py
"""

def parse_user(data: dict) -> dict:
    return {
        "name": data["name"],
        "age": data["age"],
        "email": data["email"].lower(),
    }


def calculate_score(users: list) -> float:
    total = sum(u["age"] for u in users)
    return total / len(users)


def process_records(records: list) -> list:
    results = []
    for record in records:
        user = parse_user(record)
        score = calculate_score(results)  # bug: divides by zero when results is empty
        results.append({**user, "score": score})
    return results


if __name__ == "__main__":
    sample = [
        {"name": "Alice", "age": 30, "email": "Alice@example.com"},
        {"name": "Bob",   "age": 25, "email": "Bob@example.com"},
        {"name": "Carol", "age": 28},          # bug: missing "email" key
    ]
    output = process_records(sample)
    print(output)
