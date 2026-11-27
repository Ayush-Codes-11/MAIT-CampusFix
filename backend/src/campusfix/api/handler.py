"""AWS API Gateway HTTP API v2 lambda handler for MAIT CampusFix."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, Callable

from botocore.exceptions import ClientError

from campusfix.api.dto import complaint_to_dto
from campusfix.api.response import (
    build_cors_preflight_response,
    build_json_response,
    build_problem_response,
    get_cors_headers,
)
from campusfix.api.router import Router
from campusfix.domain.models import Actor, ComplaintStatus, IssueCategory, ValidationError
from campusfix.domain.rules import (
    ActiveFollowUpExistsError,
    ComplaintAlreadyExistsError,
    ComplaintNotFoundError,
    DestinationConflictError,
    FeedbackNotAllowedError,
    FollowUpEligibilityError,
    ImmutableFieldViolationError,
    InvalidFeedbackRatingError,
    InvalidFollowUpReasonError,
    InvalidStatusTransitionError,
    MissingResolutionRemarksError,
    OptimisticLockError,
    StaleLeaseError,
    StorageThrottleError,
    TerminalStatusError,
    UnauthorizedError,
    UploadFailedError,
    UploadIntentExpiredError,
    UploadIntentNotFoundError,
    UploadNotReadyError,
)
from campusfix.bootstrap import create_application
from campusfix.middleware import ForbiddenError, extract_verified_actor
from campusfix.services.boto3_s3_storage import (
    StagingObjectDeletedError,
    UploadAmbiguousVersionError,
)
from campusfix.services.complaints import ComplaintService
from campusfix.services.uploads import UploadIntentService

logger = logging.getLogger(__name__)

_THROTTLE_ERROR_CODES = frozenset(
    {
        "SlowDown",
        "ServiceUnavailable",
        "Throttling",
        "ThrottlingException",
        "RequestLimitExceeded",
        "ProvisionedThroughputExceededException",
    }
)


def _parse_event_body(event: dict[str, Any]) -> dict[str, Any]:
    """Safely decode and parse request body from API Gateway event."""
    raw_body = event.get("body")
    if not raw_body:
        return {}

    if event.get("isBase64Encoded"):
        try:
            raw_body = base64.b64decode(raw_body).decode("utf-8")
        except Exception as err:
            raise ValidationError("Malformed base64-encoded request body.") from err

    if isinstance(raw_body, str):
        cleaned = raw_body.strip()
        if not cleaned:
            return {}
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as err:
            raise ValidationError(f"Malformed JSON in request body: {err.msg}.") from err
        if not isinstance(parsed, dict):
            raise ValidationError(f"JSON request body must be an object, got {type(parsed).__name__}.")
        return parsed

    if isinstance(raw_body, dict):
        return raw_body

    raise ValidationError(f"Unexpected request body type: {type(raw_body).__name__}.")


def create_api_handler(
    complaints_service: ComplaintService,
    uploads_service: UploadIntentService | None = None,
) -> Callable[[dict[str, Any], Any], dict[str, Any]]:
    """Factory creating an API Gateway HTTP API v2 lambda handler with injected services."""
    router = Router()

    # Route 1: List complaints
    def handle_list_complaints(actor: Actor, path_params: dict, query_params: dict, body: dict) -> dict[str, Any]:
        raw_status = query_params.get("status")
        status_enum = None
        if raw_status:
            try:
                status_enum = ComplaintStatus(raw_status)
            except ValueError:
                valid = [s.value for s in ComplaintStatus]
                raise ValidationError(f"Invalid status filter '{raw_status}'. Allowed: {', '.join(valid)}.")

        raw_category = query_params.get("category")
        category_enum = None
        if raw_category:
            try:
                category_enum = IssueCategory(raw_category)
            except ValueError:
                valid = [c.value for c in IssueCategory]
                raise ValidationError(f"Invalid category filter '{raw_category}'. Allowed: {', '.join(valid)}.")

        building = query_params.get("building")
        limit_str = query_params.get("limit", "20")
        try:
            limit = int(limit_str)
        except ValueError:
            raise ValidationError(f"Pagination limit must be an integer, got {limit_str!r}.")

        cursor = query_params.get("cursor")

        items, next_cursor = complaints_service.list_complaints_paginated(
            status=status_enum,
            building=building,
            category=category_enum,
            limit=limit,
            cursor=cursor,
        )

        return build_json_response(
            200,
            {
                "items": [complaint_to_dto(c, actor) for c in items],
                "nextCursor": next_cursor,
            },
        )

    # Route 2: Create complaint
    def handle_create_complaint(actor: Actor, path_params: dict, query_params: dict, body: dict) -> dict[str, Any]:
        title = body.get("title")
        description = body.get("description")
        category = body.get("category")
        building = body.get("building")
        floor = body.get("floor")
        room_number = body.get("roomNumber", body.get("room_number"))

        if not title or not description or not category or not building or not floor or not room_number:
            raise ValidationError("Fields 'title', 'description', 'category', 'building', 'floor', and 'roomNumber' are required.")

        complaint = complaints_service.create_complaint(
            title=title,
            description=description,
            building=building,
            floor=floor,
            room_number=room_number,
            category=category,
            actor=actor,
        )
        return build_json_response(201, complaint_to_dto(complaint, actor))

    # Route 3: Get complaint details
    def handle_get_complaint(actor: Actor, path_params: dict, query_params: dict, body: dict) -> dict[str, Any]:
        cid = path_params["id"]
        complaint = complaints_service.get_complaint_or_raise(cid)
        return build_json_response(200, complaint_to_dto(complaint, actor))

    # Route 4: Transition complaint status
    def handle_patch_status(actor: Actor, path_params: dict, query_params: dict, body: dict) -> dict[str, Any]:
        cid = path_params["id"]
        to_status = body.get("status")
        remarks = body.get("remarks")
        expected_version = body.get("expectedVersion", body.get("expected_version"))

        if not to_status:
            raise ValidationError("Field 'status' is required for status transition.")

        complaint = complaints_service.transition_status(
            complaint_id=cid,
            to_status=to_status,
            actor=actor,
            remarks=remarks,
            expected_version=expected_version,
        )
        return build_json_response(200, complaint_to_dto(complaint, actor))

    # Route 5: Assign maintenance staff
    def handle_patch_staff(actor: Actor, path_params: dict, query_params: dict, body: dict) -> dict[str, Any]:
        cid = path_params["id"]
        staff_name = body.get("assignedStaff", body.get("assigned_staff"))
        expected_version = body.get("expectedVersion", body.get("expected_version"))

        if not staff_name:
            raise ValidationError("Field 'assignedStaff' is required for staff assignment.")

        complaint = complaints_service.assign_staff(
            complaint_id=cid,
            staff_name=staff_name,
            actor=actor,
            expected_version=expected_version,
        )
        return build_json_response(200, complaint_to_dto(complaint, actor))

    # Route 6: Idempotent Add Upvote (PUT)
    def handle_put_upvote(actor: Actor, path_params: dict, query_params: dict, body: dict) -> dict[str, Any]:
        cid = path_params["id"]
        complaint, _ = complaints_service.add_upvote(complaint_id=cid, actor=actor)
        return build_json_response(
            200,
            {
                "complaintId": complaint.id,
                "upvotes": len(complaint.upvotes),
                "hasUpvoted": True,
            },
        )

    # Route 7: Idempotent Remove Upvote (DELETE)
    def handle_delete_upvote(actor: Actor, path_params: dict, query_params: dict, body: dict) -> dict[str, Any]:
        cid = path_params["id"]
        complaint, _ = complaints_service.remove_upvote(complaint_id=cid, actor=actor)
        return build_json_response(
            200,
            {
                "complaintId": complaint.id,
                "upvotes": len(complaint.upvotes),
                "hasUpvoted": False,
            },
        )

    # Route 8: Submit feedback
    def handle_post_feedback(actor: Actor, path_params: dict, query_params: dict, body: dict) -> dict[str, Any]:
        cid = path_params["id"]
        rating = body.get("rating")
        comment = body.get("comment", body.get("comments"))
        expected_version = body.get("expectedVersion", body.get("expected_version"))

        if rating is None or isinstance(rating, bool) or not isinstance(rating, int):
            raise ValidationError("Field 'rating' is required and must be an integer between 1 and 5.")

        complaint = complaints_service.submit_feedback(
            complaint_id=cid,
            rating=rating,
            actor=actor,
            comment=comment,
            expected_version=expected_version,
        )
        return build_json_response(200, complaint_to_dto(complaint, actor))

    # Route 9: Create follow-up
    def handle_post_follow_up(actor: Actor, path_params: dict, query_params: dict, body: dict) -> dict[str, Any]:
        cid = path_params["id"]
        reason = body.get("reason", body.get("followUpReason"))
        title = body.get("title")

        if not reason:
            raise ValidationError("Field 'reason' is required to file a follow-up complaint.")

        follow_up = complaints_service.create_follow_up(
            original_complaint_id=cid,
            follow_up_reason=reason,
            actor=actor,
            title=title,
        )
        return build_json_response(201, complaint_to_dto(follow_up, actor))

    # Route 10: List linked follow-ups
    def handle_get_follow_ups(actor: Actor, path_params: dict, query_params: dict, body: dict) -> dict[str, Any]:
        cid = path_params["id"]
        follow_ups = complaints_service.get_linked_follow_ups(cid)
        return build_json_response(200, [complaint_to_dto(c, actor) for c in follow_ups])

    # Route 11: Create upload intent
    def handle_post_upload_intent(actor: Actor, path_params: dict, query_params: dict, body: dict) -> dict[str, Any]:
        if uploads_service is None:
            raise ValidationError("UploadIntentService is not configured.")

        cid = path_params["id"]
        content_type = body.get("contentType", body.get("content_type"))
        byte_size = body.get("byteSize", body.get("byte_size"))
        checksum = body.get("checksumSha256", body.get("checksum_sha256"))

        if not content_type or byte_size is None or not checksum:
            raise ValidationError("Fields 'contentType', 'byteSize', and 'checksumSha256' are required.")

        intent_res = uploads_service.create_upload_intent(
            complaint_id=cid,
            content_type=content_type,
            byte_size=byte_size,
            checksum_sha256=checksum,
            actor=actor,
        )
        return build_json_response(200, intent_res)

    # Route 12: Confirm image upload
    def handle_post_confirm_upload(actor: Actor, path_params: dict, query_params: dict, body: dict) -> dict[str, Any]:
        if uploads_service is None:
            raise ValidationError("UploadIntentService is not configured.")

        cid = path_params["id"]
        intent_id = body.get("intentId", body.get("intent_id"))
        if not intent_id:
            raise ValidationError("Field 'intentId' is required to confirm an image upload.")

        confirm_res = uploads_service.confirm_upload(
            complaint_id=cid,
            intent_id=intent_id,
            actor=actor,
        )
        return build_json_response(200, confirm_res)

    # Route 13: Get image download URL
    def handle_get_download_url(actor: Actor, path_params: dict, query_params: dict, body: dict) -> dict[str, Any]:
        if uploads_service is None:
            raise ValidationError("UploadIntentService is not configured.")

        cid = path_params["id"]
        url_res = uploads_service.get_download_url(
            complaint_id=cid,
            actor=actor,
        )
        return build_json_response(200, url_res)

    # Register all routes with Router
    router.add_route("GET", "/v1/complaints", handle_list_complaints)
    router.add_route("POST", "/v1/complaints", handle_create_complaint)
    router.add_route("GET", "/v1/complaints/{id}", handle_get_complaint)
    router.add_route("PATCH", "/v1/complaints/{id}/status", handle_patch_status)
    router.add_route("PATCH", "/v1/complaints/{id}/staff", handle_patch_staff)
    router.add_route("PUT", "/v1/complaints/{id}/upvote", handle_put_upvote)
    router.add_route("DELETE", "/v1/complaints/{id}/upvote", handle_delete_upvote)
    router.add_route("POST", "/v1/complaints/{id}/feedback", handle_post_feedback)
    router.add_route("POST", "/v1/complaints/{id}/follow-ups", handle_post_follow_up)
    router.add_route("GET", "/v1/complaints/{id}/follow-ups", handle_get_follow_ups)
    router.add_route("POST", "/v1/complaints/{id}/images/upload-intent", handle_post_upload_intent)
    router.add_route("POST", "/v1/complaints/{id}/images/confirm", handle_post_confirm_upload)
    router.add_route("GET", "/v1/complaints/{id}/images/download-url", handle_get_download_url)

    def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
        """Entry point for API Gateway HTTP API v2 lambda requests."""
        # Extract and normalize request origin
        request_origin: str | None = None
        if isinstance(event, dict):
            for k, v in (event.get("headers") or {}).items():
                if k.lower() == "origin" and isinstance(v, str):
                    request_origin = v.strip()
                    break

        def problem(status: int, title: str, detail: str, code: str) -> dict[str, Any]:
            return build_problem_response(status, title, detail, code, request_origin=request_origin)

        if not isinstance(event, dict):
            return problem(400, "Bad Request", "Malformed Lambda invocation event.", "INVALID_EVENT")

        method = (
            event.get("requestContext", {}).get("http", {}).get("method")
            or event.get("httpMethod")
            or "GET"
        ).upper()

        path = event.get("rawPath") or event.get("path") or "/"

        # 1. CORS Preflight
        if method == "OPTIONS":
            return build_cors_preflight_response(request_origin=request_origin)

        # Public Health Check
        if method == "GET" and path == "/v1/health":
            return build_json_response(200, {"status": "healthy", "service": "campusfix-backend"}, request_origin=request_origin)

        # 2. Extract and authenticate caller
        try:
            actor = extract_verified_actor(event)
        except UnauthorizedError as err:
            return problem(401, "Unauthorized", str(err), "UNAUTHORIZED")
        except ForbiddenError as err:
            return problem(403, "Forbidden", str(err), "FORBIDDEN")
        except Exception as err:
            logger.error("Authentication extraction error: %s", err)
            return problem(401, "Unauthorized", "Authentication verification failed.", "AUTH_FAILED")

        # 3. Match Route
        match = router.match(method, path)
        if match is None:
            return problem(404, "Not Found", f"Route '{method} {path}' not found.", "ROUTE_NOT_FOUND")

        # 4. Parse Body and Query
        try:
            body = _parse_event_body(event)
        except ValidationError as err:
            return problem(400, "Bad Request", str(err), "INVALID_REQUEST_BODY")

        query_params = event.get("queryStringParameters") or {}

        # 5. Dispatch Route with Exception Mapping
        try:
            response = match.handler(actor, match.path_params, query_params, body)
            if isinstance(response, dict) and "headers" in response:
                response["headers"].update(get_cors_headers(request_origin))
            return response
        except (
            ValidationError,
            InvalidStatusTransitionError,
            TerminalStatusError,
            InvalidFollowUpReasonError,
            FollowUpEligibilityError,
            FeedbackNotAllowedError,
            InvalidFeedbackRatingError,
            MissingResolutionRemarksError,
            UploadNotReadyError,
            UploadFailedError,
            UploadIntentExpiredError,
            StagingObjectDeletedError,
        ) as err:
            return problem(400, "Bad Request", str(err), "VALIDATION_ERROR")
        except UnauthorizedError as err:
            # Service-level UnauthorizedError represents an authenticated actor failing a permission/ownership check
            return problem(403, "Forbidden", str(err), "FORBIDDEN")
        except (ComplaintNotFoundError, UploadIntentNotFoundError) as err:
            return problem(404, "Not Found", str(err), "RESOURCE_NOT_FOUND")
        except (
            OptimisticLockError,
            ActiveFollowUpExistsError,
            ComplaintAlreadyExistsError,
            StaleLeaseError,
            DestinationConflictError,
            ImmutableFieldViolationError,
            UploadAmbiguousVersionError,
        ) as err:
            return problem(409, "Conflict", str(err), "CONFLICT")
        except StorageThrottleError:
            logger.warning("Transient storage throttling processing '%s %s'", method, path)
            return problem(
                503,
                "Service Unavailable",
                "Storage service is temporarily throttled. Please retry shortly.",
                "SERVICE_UNAVAILABLE",
            )
        except ClientError as err:
            error_info = err.response.get("Error", {}) if isinstance(err.response, dict) else {}
            error_code = str(error_info.get("Code", ""))
            http_status = (
                err.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                if isinstance(err.response, dict)
                else None
            )
            if error_code in _THROTTLE_ERROR_CODES or http_status == 503:
                logger.warning(
                    "Transient AWS storage throttling (%s) processing '%s %s'",
                    error_code,
                    method,
                    path,
                )
                return problem(
                    503,
                    "Service Unavailable",
                    "Storage service is temporarily throttled. Please retry shortly.",
                    "SERVICE_UNAVAILABLE",
                )
            logger.exception("Unhandled AWS client error processing '%s %s'", method, path)
            return problem(
                500,
                "Internal Server Error",
                "An unexpected internal server error occurred.",
                "INTERNAL_SERVER_ERROR",
            )
        except Exception as err:
            # Sanitized internal server error — never leak internal stack traces or database info
            logger.exception("Unhandled server exception processing '%s %s'", method, path)
            return problem(
                500,
                "Internal Server Error",
                "An unexpected internal server error occurred.",
                "INTERNAL_SERVER_ERROR",
            )

    return lambda_handler


def create_api_handler_from_env(
    **bootstrap_kwargs: Any,
) -> Callable[[dict[str, Any], Any], dict[str, Any]]:
    """Compose services via ``create_application`` and return a configured Lambda API handler."""
    app = create_application(**bootstrap_kwargs)
    return create_api_handler(
        complaints_service=app.complaints_service,
        uploads_service=app.uploads_service,
    )
