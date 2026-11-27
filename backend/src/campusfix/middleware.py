"""Authentication and authorization middleware for MAIT CampusFix API Gateway HTTP API.

This module provides verified identity and role extraction from incoming AWS API Gateway
HTTP API ($v2$) request events that have been authenticated by the native API Gateway
JWT Authorizer against Amazon Cognito User Pools.

Security Architecture:
- Cryptographic signature, issuer, audience/client ID, and token expiration validation
  are executed at the API Gateway edge.
- This middleware performs application-level claims parsing, token use validation,
  identity extraction, and fail-closed role assignment.
- Claims path: event["requestContext"]["authorizer"]["jwt"]["claims"]
- Enforces token_use == "access" (strictly rejecting ID tokens for API authorization).
- Extracts sub as the immutable, verified caller user_id.
- Never silently normalizes identity claims (rejects sub with leading/trailing whitespace).
- Evaluates cognito:groups for mutually exclusive role mapping:
    - 'students'    -> Role.STUDENT
    - 'maintenance' -> Role.MAINTENANCE
    - 'admins'      -> Role.ADMIN
- Rejects missing, empty, unknown, malformed, or conflicting multi-group memberships.
- Does not assume or infer verified email status merely from successful authentication.
- Never accepts or inspects client-supplied request body, query parameters, or headers
  for identity or role claims.
"""

from __future__ import annotations

import json
from typing import Any, Final

from campusfix.domain.models import Actor, CampusFixDomainError, Role
from campusfix.domain.rules import UnauthorizedError


class ForbiddenError(CampusFixDomainError, PermissionError):
    """Raised when an authenticated principal lacks required groups or presents conflicting roles (HTTP 403)."""


# Supported Cognito group name to domain Role mapping
GROUP_TO_ROLE: Final[dict[str, Role]] = {
    "students": Role.STUDENT,
    "maintenance": Role.MAINTENANCE,
    "admins": Role.ADMIN,
}


def _extract_groups(raw_groups: Any) -> set[str]:
    """Parse and validate raw cognito:groups claim into a set of clean group strings.

    Supports:
    - Direct Python list (or tuple/set) of strings from API Gateway JSON deserialization.
    - JSON-encoded array string (e.g. '["students"]') parsed via json.loads().
    - Exact bare recognized group name string (e.g. "students", "maintenance", "admins").

    Raises:
        ForbiddenError: If groups claim is missing, empty, malformed JSON, contains unmatched
            brackets/quotes, empty/whitespace elements, duplicate entries, unexpected separators,
            nested structures, or unexpected types.
    """
    if raw_groups is None:
        raise ForbiddenError("Missing 'cognito:groups' claim in token: user has no assigned role.")

    groups_list: list[str] = []

    if isinstance(raw_groups, (list, tuple, set)):
        if not raw_groups:
            raise ForbiddenError("Empty 'cognito:groups' claim in token: user has no assigned role.")
        for item in raw_groups:
            if not isinstance(item, str):
                raise ForbiddenError(
                    f"Malformed entry in 'cognito:groups': expected str, got {type(item).__name__}."
                )
            if not item or item != item.strip():
                raise ForbiddenError(
                    "Malformed entry in 'cognito:groups': contains empty or whitespace-padded group name."
                )
            groups_list.append(item)

    elif isinstance(raw_groups, str):
        cleaned = raw_groups.strip()
        if not cleaned:
            raise ForbiddenError("Empty 'cognito:groups' claim in token: user has no assigned role.")

        if cleaned.startswith("["):
            # Must be a valid JSON-encoded array
            try:
                parsed = json.loads(cleaned)
            except (json.JSONDecodeError, ValueError) as err:
                raise ForbiddenError(
                    f"Malformed JSON array in 'cognito:groups': {raw_groups!r}."
                ) from err

            if not isinstance(parsed, list):
                raise ForbiddenError(
                    f"Malformed 'cognito:groups': JSON value must be an array, got {type(parsed).__name__}."
                )
            if not parsed:
                raise ForbiddenError("Empty JSON array in 'cognito:groups': user has no assigned role.")

            for item in parsed:
                if not isinstance(item, str):
                    raise ForbiddenError(
                        f"Malformed entry in 'cognito:groups' JSON array: expected str, got {type(item).__name__}."
                    )
                if not item or item != item.strip():
                    raise ForbiddenError(
                        "Malformed entry in 'cognito:groups' JSON array: contains empty or whitespace-padded group name."
                    )
                groups_list.append(item)

        elif cleaned in GROUP_TO_ROLE:
            # Exact bare recognized group name
            if raw_groups != cleaned:
                raise ForbiddenError(
                    f"Malformed 'cognito:groups' string contains invalid surrounding whitespace: {raw_groups!r}."
                )
            groups_list.append(cleaned)

        else:
            raise ForbiddenError(
                f"Unrecognized or malformed group claim in 'cognito:groups': {raw_groups!r}."
            )

    else:
        raise ForbiddenError(
            f"Malformed 'cognito:groups' claim: unexpected type {type(raw_groups).__name__}."
        )

    # Check for duplicate entries
    if len(groups_list) != len(set(groups_list)):
        raise ForbiddenError(
            f"Duplicate group entries in 'cognito:groups' are not permitted: {groups_list}."
        )

    groups_set = set(groups_list)
    if not groups_set:
        raise ForbiddenError("Empty 'cognito:groups' claim in token: user has no assigned role.")

    return groups_set


def extract_verified_actor(event: dict[str, Any]) -> Actor:
    """Extract and validate the authenticated Actor from an API Gateway HTTP API v2 event.

    Args:
        event: The AWS Lambda event dictionary dispatched by API Gateway HTTP API (payload format v2).

    Returns:
        Actor: Immutable authenticated principal containing verified user_id and Role.

    Raises:
        UnauthorizedError: If event structure is missing or malformed, claims are missing,
            token_use != 'access' (e.g. ID token presented), or sub claim is missing/invalid (HTTP 401).
        ForbiddenError: If cognito:groups is missing, empty, unrecognized, malformed, or contains
            conflicting multiple role groups (HTTP 403).
    """
    if not isinstance(event, dict):
        raise UnauthorizedError(
            f"Invalid event structure: expected dictionary, got {type(event).__name__}."
        )

    request_context = event.get("requestContext")
    if not isinstance(request_context, dict):
        raise UnauthorizedError("Missing or invalid 'requestContext' in request event.")

    authorizer = request_context.get("authorizer")
    if not isinstance(authorizer, dict):
        raise UnauthorizedError("Missing or invalid 'authorizer' context in request event.")

    jwt_context = authorizer.get("jwt")
    if not isinstance(jwt_context, dict):
        raise UnauthorizedError("Missing or invalid 'jwt' authorizer context in request event.")

    claims = jwt_context.get("claims")
    if not isinstance(claims, dict):
        raise UnauthorizedError("Missing or invalid 'claims' dictionary in JWT authorizer context.")

    # 1. Enforce token_use == "access"
    token_use = claims.get("token_use")
    if token_use is None:
        raise UnauthorizedError("Missing 'token_use' claim in access token.")
    if not isinstance(token_use, str):
        raise UnauthorizedError(
            f"Malformed 'token_use' claim: expected string, got {type(token_use).__name__}."
        )
    if token_use != "access":
        if token_use == "id":
            raise UnauthorizedError(
                "ID tokens are not permitted for API authorization; expected token_use='access'."
            )
        raise UnauthorizedError(f"Invalid token_use '{token_use}'; expected 'access'.")

    # 2. Extract and validate sub
    sub = claims.get("sub")
    if sub is None:
        raise UnauthorizedError("Missing 'sub' claim in token claims.")
    if not isinstance(sub, str):
        raise UnauthorizedError(f"Malformed 'sub' claim: expected string, got {type(sub).__name__}.")
    if not sub or not sub.strip():
        raise UnauthorizedError("Empty or whitespace-only 'sub' claim in token claims.")
    if sub != sub.strip():
        raise UnauthorizedError("Malformed 'sub' claim contains invalid leading or trailing whitespace.")

    # 3. Extract, validate, and map cognito:groups
    raw_groups = claims.get("cognito:groups")
    groups_set = _extract_groups(raw_groups)

    # Check for unrecognized groups
    unknown_groups = groups_set - set(GROUP_TO_ROLE.keys())
    if unknown_groups:
        raise ForbiddenError(
            f"Unrecognized group(s) in 'cognito:groups': {sorted(unknown_groups)}. "
            f"Authorized groups are: {sorted(GROUP_TO_ROLE.keys())}."
        )

    # Enforce mutual exclusivity (exactly one recognized role group)
    if len(groups_set) > 1:
        raise ForbiddenError(
            f"Conflicting multiple role groups in 'cognito:groups': {sorted(groups_set)}. "
            f"Users must belong to exactly one role group."
        )

    group_name = next(iter(groups_set))
    role = GROUP_TO_ROLE[group_name]

    # Construct and return authentic Actor with exact unmodified sub
    return Actor(user_id=sub, role=role)
