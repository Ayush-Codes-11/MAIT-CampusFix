"""Amazon Cognito Post-Confirmation Lambda Trigger for MAIT CampusFix.

Enrolls newly confirmed self-registered students into the 'students' Cognito group.
Only fires for PostConfirmation_ConfirmSignUp; other trigger sources return the
event without invoking any Cognito APIs.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

STUDENT_GROUP_NAME = "students"
CONFIRM_SIGN_UP_TRIGGER = "PostConfirmation_ConfirmSignUp"

# Module-level cached Cognito client for Lambda execution environment reuse.
_cached_client: Any = None


def _get_default_cognito_client() -> Any:
    """Retrieve or initialize the default boto3 Cognito IDP client."""
    global _cached_client
    if _cached_client is None:
        try:
            import boto3
        except ImportError as err:
            raise RuntimeError(
                "boto3 is required for Cognito operations but is not installed."
            ) from err
        _cached_client = boto3.client("cognito-idp")
    return _cached_client


def lambda_handler(
    event: dict[str, Any],
    context: Any = None,
    cognito_client: Any = None,
) -> dict[str, Any]:
    """Enroll newly confirmed students into the 'students' Cognito group.

    Args:
        event: Cognito trigger event payload.
        context: Lambda execution context (unused).
        cognito_client: Optional injected boto3 client for unit testing.

    Returns:
        The event dictionary.

    Raises:
        ValueError: If event payload is missing required identifiers.
        ClientError: If the Cognito API call fails.
    """
    if not isinstance(event, dict):
        raise ValueError("Invalid event payload: expected a dictionary.")

    trigger_source = event.get("triggerSource")

    # Only process self-registration confirmation. Other trigger sources
    # (e.g., PostConfirmation_ConfirmForgotPassword) must return without calling Cognito.
    if trigger_source != CONFIRM_SIGN_UP_TRIGGER:
        logger.info("Ignoring non-signup trigger source: %s", trigger_source)
        return event

    user_pool_id = event.get("userPoolId")
    username = event.get("userName")

    if not isinstance(user_pool_id, str) or not user_pool_id.strip():
        logger.error(
            "Invalid event payload: missing or invalid userPoolId for triggerSource '%s'.",
            trigger_source,
        )
        raise ValueError("Invalid event payload: missing or invalid userPoolId.")

    if not isinstance(username, str) or not username.strip():
        logger.error(
            "Invalid event payload: missing or invalid userName for triggerSource '%s'.",
            trigger_source,
        )
        raise ValueError("Invalid event payload: missing or invalid userName.")

    client = cognito_client if cognito_client is not None else _get_default_cognito_client()

    try:
        # Cognito's AdminAddUserToGroup is natively idempotent: calling it on a user
        # that is already a member of the group succeeds and returns HTTP 200 OK.
        client.admin_add_user_to_group(
            UserPoolId=user_pool_id.strip(),
            Username=username.strip(),
            GroupName=STUDENT_GROUP_NAME,
        )
        logger.info(
            "Successfully completed group assignment to '%s' in user pool '%s'.",
            STUDENT_GROUP_NAME,
            user_pool_id.strip(),
        )
    except Exception as err:
        error_type = type(err).__name__
        error_code = None
        if hasattr(err, "response") and isinstance(getattr(err, "response", None), dict):
            error_code = err.response.get("Error", {}).get("Code")
        err_identifier = f"{error_type}:{error_code}" if error_code else error_type

        logger.error(
            "Failed group assignment operation for group '%s' in user pool '%s' "
            "(triggerSource: '%s', error: %s).",
            STUDENT_GROUP_NAME,
            user_pool_id.strip(),
            trigger_source,
            err_identifier,
        )
        # Re-raise to record error in CloudWatch logs and metrics unchanged
        raise

    return event
