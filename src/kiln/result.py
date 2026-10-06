"""The result stored on a job after a successful execution."""


def envelope(
    *,
    summary: str,
    evidence: list[str] | None = None,
    artifacts: list[dict] | None = None,
    role_result: dict | None = None,
) -> dict:
    return {
        "summary": summary,
        "evidence": list(evidence or []),
        "artifacts": list(artifacts or []),
        "role_result": dict(role_result or {}),
    }


def role_result(report: dict) -> dict:
    """The role-specific section of an envelope, or the report itself for an older shape."""
    nested = report.get("role_result")
    if isinstance(nested, dict):
        return nested
    return report
