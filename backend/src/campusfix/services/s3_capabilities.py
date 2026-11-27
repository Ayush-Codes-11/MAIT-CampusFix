"""AWS SDK capability introspection checks for MAIT CampusFix S3 uploads."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from campusfix.domain.rules import CampusFixDomainError


class SdkCapabilityError(CampusFixDomainError):
    """Raised when the installed AWS SDK lacks required conditional-write support."""


def _load_s3_service_model(s3_client: Any | None = None) -> Any:
    """Resolve the Botocore S3 service model locally without making network calls."""
    if s3_client is not None:
        service_model = getattr(s3_client, "_service_model", None)
        if service_model is None:
            raise SdkCapabilityError(
                "Provided S3 client does not expose a Botocore _service_model; failing closed."
            )
        return service_model

    try:
        import botocore.session
    except ImportError as err:
        raise SdkCapabilityError(
            "botocore is not installed; cannot verify CopyObjectRequest.IfNoneMatch support."
        ) from err

    try:
        return botocore.session.get_session().get_service_model("s3")
    except Exception as err:
        raise SdkCapabilityError(
            f"Unable to load local Botocore S3 service model: {err}"
        ) from err


def assert_copy_if_none_match_supported(s3_client: Any | None = None) -> None:
    """Fail closed unless Botocore's S3 CopyObject model declares destination IfNoneMatch.

    DynamoDB OCC and lease fencing cannot prevent delayed S3 overwrites on their own,
    so destination CopyObject must always pass IfNoneMatch='*' mapped to the HTTP
    header 'If-None-Match'. Unconditional CopyObject fallback is strictly forbidden.
    """
    service_model = _load_s3_service_model(s3_client)

    try:
        op_model = service_model.operation_model("CopyObject")
    except Exception as err:
        raise SdkCapabilityError(
            f"S3 service model missing CopyObject operation definition: {err}"
        ) from err

    input_shape = getattr(op_model, "input_shape", None)
    members = getattr(input_shape, "members", None)
    if members is None or not hasattr(members, "__contains__"):
        raise SdkCapabilityError(
            "CopyObject input_shape does not expose a members collection; failing closed."
        )

    if "IfNoneMatch" not in members:
        raise SdkCapabilityError(
            "Installed botocore CopyObjectRequest model lacks 'IfNoneMatch'. "
            "Pin a compatible boto3/botocore version supporting destination IfNoneMatch; "
            "unconditional CopyObject fallback is forbidden."
        )

    # Guard against mistaking CopySourceIfNoneMatch or another shape for If-None-Match.
    if isinstance(members, Mapping):
        if_none_match_shape = members["IfNoneMatch"]
        serialization = getattr(if_none_match_shape, "serialization", None)
        if isinstance(serialization, Mapping) and (
            serialization.get("location") != "header"
            or serialization.get("name") != "If-None-Match"
        ):
            raise SdkCapabilityError(
                "CopyObjectRequest.IfNoneMatch serialization is invalid "
                f"(got {dict(serialization)!r}, expected HTTP header 'If-None-Match')."
            )
