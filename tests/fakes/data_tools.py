"""A data tool whose result is large and carries ids, for history masking tests."""

import json

from strands import tool


@tool
def run_query(sql: str) -> str:
    """Run a SQL query and return its rows."""
    rows = [{"region": f"region-{n}", "revenue": n * 1000} for n in range(200)]
    return json.dumps({"queryExecutionId": "q-123", "sql": sql, "rows": rows})
