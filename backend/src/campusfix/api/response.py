"""HTTP Response builders and RFC 7807 Problem Details formatting for MAIT CampusFix API."""

from __future__ import annotations

import json
from typing import Any

# Permitted local development origins required by the project.
# Production origins must not be added until explicitly confirmed.
ALLOWED_ORIGINS: frozenset[str] = frozenset({
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
})

CORS_ALLOW_METHODS = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
CORS_ALLOW_HEADERS = "Authorization, Content-Type, Idempotency-Key"


def get_cors_headers(request_origin: str | None = None) -> dict[str, str]:
    """Generate CORS response headers based on the validated request origin.
    
    Rules:
    - Never returns wildcard '*' origin.
    - If request_origin exactly matches an allowlisted origin:
      returns Access-Control-Allow-Origin, Access-Control-Allow-Methods,
      Access-Control-Allow-Headers, and Vary: Origin.
    - If request_origin is absent or not in the allowlist:
      does NOT return Access-Control-Allow-Origin, but returns Vary: Origin.
    """
    headers: dict[str, str] = {
        "Vary": "Origin",
    }
    if request_origin and request_origin.strip() in ALLOWED_ORIGINS:
        matched_origin = request_origin.strip()
        headers["Access-Control-Allow-Origin"] = matched_origin
        headers["Access-Control-Allow-Methods"] = CORS_ALLOW_METHODS
        headers["Access-Control-Allow-Headers"] = CORS_ALLOW_HEADERS
    return headers


def build_json_response(
    status_code: int,
    data: Any,
    headers: dict[str, str] | None = None,
    request_origin: str | None = None,
) -> dict[str, Any]:
    """Construct a standard API Gateway HTTP API v2 JSON response."""
    resp_headers = {
        "Content-Type": "application/json",
    }
    resp_headers.update(get_cors_headers(request_origin))
    if headers:
        resp_headers.update(headers)

    body_str = json.dumps(data) if data is not None else ""
    return {
        "statusCode": status_code,
        "headers": resp_headers,
        "body": body_str,
        "isBase64Encoded": False,
    }


def build_problem_response(
    status: int,
    title: str,
    detail: str,
    code: str,
    headers: dict[str, str] | None = None,
    request_origin: str | None = None,
) -> dict[str, Any]:
    """Construct an RFC 7807 Problem Details JSON response with an extension 'code' field."""
    problem = {
        "type": "about:blank",
        "title": title,
        "status": status,
        "detail": detail,
        "code": code,
    }
    return build_json_response(status, problem, headers=headers, request_origin=request_origin)


def build_cors_preflight_response(request_origin: str | None = None) -> dict[str, Any]:
    """Return a 204 No Content response for OPTIONS preflight requests."""
    resp_headers = get_cors_headers(request_origin)
    return {
        "statusCode": 204,
        "headers": resp_headers,
        "body": "",
        "isBase64Encoded": False,
    }
