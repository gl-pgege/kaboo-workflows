from strands.tools.decorator import tool


@tool
def calculator(expression: str) -> str:
    """Evaluate a math expression and return the result.

    Args:
        expression: A mathematical expression to evaluate (e.g. "15 * 23").

    Returns:
        The computed result as a string, or an error message.
    """
    try:
        result = eval(expression)  # noqa: S307 # nosec B307
        return f"Result: {result}"
    except Exception as e:
        return f"Error: {e}"
