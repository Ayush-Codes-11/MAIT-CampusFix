"""Amazon Cognito Pre-Sign-up Lambda Trigger for MAIT CampusFix.

Validates sign-up email addresses against organizational domain allowlists
to enforce role-based registration boundaries:
- Public self-registration (PreSignUp_SignUp) is strictly restricted to
  verified student domains (e.g., @cst.mait.ac.in). Faculty/staff domains
  (@mait.ac.in) and public domains (@gmail.com, etc.) are rejected.
- Administrative creation (PreSignUp_AdminCreateUser) permits staff domains
  and student domains, while rejecting unauthorized external domains.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from ..domain.models import CampusFixDomainError

logger = logging.getLogger(__name__)

DEFAULT_STUDENT_DOMAINS = "cst.mait.ac.in"
DEFAULT_STAFF_DOMAINS = "mait.ac.in"


class PreSignUpFailedException(CampusFixDomainError):
    """Raised when a Cognito Pre-Sign-up verification rule is violated."""


def _get_student_allowlist() -> frozenset[str]:
    """Retrieve the set of allowed student email domains."""
    raw = os.environ.get("ALLOWED_STUDENT_EMAIL_DOMAINS", DEFAULT_STUDENT_DOMAINS)
    return frozenset(d.strip().lower() for d in raw.split(",") if d.strip())


def _get_staff_allowlist() -> frozenset[str]:
    """Retrieve the set of allowed staff email domains."""
    raw = os.environ.get("ALLOWED_STAFF_EMAIL_DOMAINS", DEFAULT_STAFF_DOMAINS)
    return frozenset(d.strip().lower() for d in raw.split(",") if d.strip())


def _validate_and_parse_email(raw_email: Any) -> tuple[str, str]:
    """Validate email format strictly and return (local_part, normalized_domain).

    Rules:
    - Must be a non-empty string.
    - No whitespace permitted anywhere in the address (no silent stripping).
    - Exactly one '@' character.
    - Non-empty local part and domain.
    - Malformed domain checks (no leading/trailing dots or consecutive dots).
    - Domain must contain at least two labels separated by dots.
    - Each domain label must be non-empty, contain only ASCII letters, digits, and hyphens,
      and must not begin or end with a hyphen.
    - Only the domain is normalized to lowercase for allowlist comparison.
    - Local part is preserved exactly.

    Raises:
        PreSignUpFailedException: If any validation rule fails.
    """
    if not isinstance(raw_email, str) or not raw_email:
        raise PreSignUpFailedException("Email attribute is required.")

    if any(c.isspace() for c in raw_email):
        raise PreSignUpFailedException("Invalid email address format: whitespace is not permitted.")

    if raw_email.count("@") != 1:
        raise PreSignUpFailedException(
            "Invalid email address format: exactly one '@' character required."
        )

    local_part, raw_domain = raw_email.split("@")

    if not local_part:
        raise PreSignUpFailedException("Invalid email address format: local part cannot be empty.")

    if not raw_domain:
        raise PreSignUpFailedException("Invalid email address format: domain cannot be empty.")

    if raw_domain.startswith(".") or raw_domain.endswith(".") or ".." in raw_domain:
        raise PreSignUpFailedException("Invalid email address format: malformed domain name.")

    labels = raw_domain.split(".")
    if len(labels) < 2:
        raise PreSignUpFailedException(
            "Invalid email address format: domain must contain at least two labels."
        )

    for label in labels:
        if not label:
            raise PreSignUpFailedException(
                "Invalid email address format: domain labels cannot be empty."
            )
        if label.startswith("-") or label.endswith("-"):
            raise PreSignUpFailedException(
                "Invalid email address format: domain labels cannot start or end with a hyphen."
            )
        if not all(c.isalnum() or c == "-" for c in label) or not label.isascii():
            raise PreSignUpFailedException(
                "Invalid email address format: domain labels must contain only ASCII letters, digits, and hyphens."
            )

    return local_part, raw_domain.lower()


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Inspect and validate sign-up email domain based on trigger source.

    Args:
        event: Cognito trigger event payload.
        context: Lambda execution context (unused).

    Returns:
        The (possibly updated) event dictionary.

    Raises:
        PreSignUpFailedException: If validation fails or trigger source is unsupported.
    """
    if not isinstance(event, dict):
        raise PreSignUpFailedException("Event payload must be a dictionary.")

    trigger_source = event.get("triggerSource")
    request = event.get("request")
    if not isinstance(request, dict):
        request = {}

    user_attrs = request.get("userAttributes")
    if not isinstance(user_attrs, dict):
        user_attrs = {}

    raw_email = user_attrs.get("email")
    _, domain = _validate_and_parse_email(raw_email)

    student_allowlist = _get_student_allowlist()
    staff_allowlist = _get_staff_allowlist()

    # -------------------------------------------------------------------------
    # Case 1: Public Self-Registration (PreSignUp_SignUp)
    # -------------------------------------------------------------------------
    if trigger_source == "PreSignUp_SignUp":
        if domain in student_allowlist:
            response = event.get("response")
            if not isinstance(response, dict):
                response = {}
                event["response"] = response

            # Explicitly require email verification code challenge;
            # never automatically confirm or verify users through public signup.
            response["autoConfirmUser"] = False
            response["autoVerifyEmail"] = False
            return event

        if domain in staff_allowlist:
            logger.warning(
                "Public self-registration rejected for staff domain '@%s'", domain
            )
            raise PreSignUpFailedException(
                "Faculty and staff accounts cannot self-register. "
                "Contact the MAIT Estate Office for account provisioning."
            )

        allowed_str = ", ".join(sorted(student_allowlist))
        logger.warning(
            "Public self-registration rejected for unauthorized domain '@%s'", domain
        )
        raise PreSignUpFailedException(
            f"Domain '@{domain}' is not authorized for student self-registration. "
            f"Authorized domains: {allowed_str}."
        )

    # -------------------------------------------------------------------------
    # Case 2: Administrative Provisioning (PreSignUp_AdminCreateUser)
    # -------------------------------------------------------------------------
    if trigger_source == "PreSignUp_AdminCreateUser":
        if domain in staff_allowlist or domain in student_allowlist:
            # Note: Cognito strictly ignores autoVerifyEmail, autoConfirmUser, and
            # autoVerifyPhone in PreSignUp_AdminCreateUser responses.
            # Mailbox verification for admin-created accounts is managed exclusively
            # via Cognito administrative APIs when independently verified by an
            # administrator, never by this trigger.
            return event

        allowed_all = sorted(staff_allowlist.union(student_allowlist))
        allowed_str = ", ".join(allowed_all)
        logger.warning(
            "Admin provisioning rejected for unauthorized domain '@%s'", domain
        )
        raise PreSignUpFailedException(
            f"Admin provisioning rejected for domain '@{domain}'. "
            f"Allowed domains: {allowed_str}."
        )

    # -------------------------------------------------------------------------
    # Case 3: Unsupported Trigger Source
    # -------------------------------------------------------------------------
    logger.error("Unsupported trigger source '%s'", trigger_source)
    raise PreSignUpFailedException(f"Unsupported trigger source '{trigger_source}'.")
