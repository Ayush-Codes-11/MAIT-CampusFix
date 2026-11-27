"""Domain rules and validation functions for MAIT CampusFix.

All business validation is pure logic, completely independent of databases,
web frameworks, or external infrastructure.
"""

from __future__ import annotations

import base64
import binascii
import re
from typing import Any, Final

from .models import (
    Actor,
    CampusFixDomainError,
    Complaint,
    ComplaintStatus,
    IssueCategory,
    Role,
    ValidationError,
)


# -----------------------------------------------------------------------------
# Domain Exceptions
# -----------------------------------------------------------------------------

class InvalidStatusTransitionError(CampusFixDomainError):
    """Raised when an illegal status transition is attempted."""


class TerminalStatusError(InvalidStatusTransitionError):
    """Raised when attempting to transition out of a terminal state."""


class MissingResolutionRemarksError(CampusFixDomainError):
    """Raised when attempting to resolve a complaint without resolution remarks."""


class FeedbackNotAllowedError(CampusFixDomainError):
    """Raised when submitting feedback for an unresolved complaint."""


class InvalidFeedbackRatingError(ValidationError):
    """Raised when a feedback rating is outside the allowed range (1-5) or not an integer."""


class FollowUpEligibilityError(CampusFixDomainError):
    """Raised when creating a follow-up for a complaint that is not resolved."""


class InvalidFollowUpReasonError(ValidationError):
    """Raised when a follow-up explanation does not meet the minimum length."""


class ActiveFollowUpExistsError(CampusFixDomainError):
    """Raised when an active (unresolved) follow-up already exists."""


class UnauthorizedError(CampusFixDomainError, PermissionError):
    """Raised when an authenticated actor attempts an action outside their permissions."""


class ComplaintAlreadyExistsError(CampusFixDomainError):
    """Raised when attempting to create a complaint with an ID that already exists."""


class ComplaintNotFoundError(CampusFixDomainError, KeyError):
    """Raised when an operation targets a non-existent complaint ID."""


class OptimisticLockError(CampusFixDomainError):
    """Raised when an update operation encounters a version conflict (stale update)."""


class StaleLeaseError(OptimisticLockError):
    """Raised when an upload confirmation worker attempts to commit with a stale or stolen fencing token."""


class UploadIntentNotFoundError(CampusFixDomainError, KeyError):
    """Raised when an operation targets a non-existent upload intent."""


class UploadIntentExpiredError(CampusFixDomainError):
    """Raised when an upload intent is accessed after its confirmation or recovery deadline."""


class UploadNotReadyError(CampusFixDomainError):
    """Raised when confirmation is requested but the staging object is not yet found in S3."""


class UploadFailedError(CampusFixDomainError):
    """Raised when an upload intent enters or has entered a terminal FAILED state."""


class DestinationConflictError(CampusFixDomainError):
    """Raised when the destination S3 key already exists with mismatched content or checksum."""


class ImmutableFieldViolationError(CampusFixDomainError):
    """Raised when a repository operation detects a mutation of an immutable field.

    Used by ``pin_source_version``, ``begin_copy_attempt``, and
    ``commit_verified_attachment_versioned`` when a pinned or committed value
    would change (or on duplicate outbox insertion).  Service adapters
    (such as ``RepositoryVersionedUploadStateAdapter``) translate this domain
    error into ``CopyVerificationError`` when implementing worker ports.
    """


class StorageThrottleError(CampusFixDomainError):
    """Raised when storage/repository operations exhaust bounded retries on transient throttling or conflict."""


# -----------------------------------------------------------------------------
# Workflow Transition Matrix & Constraints
# -----------------------------------------------------------------------------

VALID_TRANSITIONS: Final[dict[ComplaintStatus, tuple[ComplaintStatus, ...]]] = {
    ComplaintStatus.PENDING: (ComplaintStatus.IN_PROGRESS,),
    ComplaintStatus.IN_PROGRESS: (ComplaintStatus.RESOLVED,),
    ComplaintStatus.RESOLVED: (),  # Terminal state
}

STATUS_LABELS: Final[dict[ComplaintStatus, str]] = {
    ComplaintStatus.PENDING: "Pending",
    ComplaintStatus.IN_PROGRESS: "In Progress",
    ComplaintStatus.RESOLVED: "Resolved",
}

MIN_FOLLOW_UP_REASON_LENGTH: Final[int] = 10
MIN_FEEDBACK_RATING: Final[int] = 1
MAX_FEEDBACK_RATING: Final[int] = 5
FIRE_SAFETY_SLA_DAYS: Final[int] = 3
DEFAULT_OVERDUE_DAYS: Final[int] = 7
MIN_OVERDUE_DAYS: Final[int] = 1
MAX_OVERDUE_DAYS: Final[int] = 30
MAX_IMAGE_BYTES: Final[int] = 5 * 1024 * 1024  # 5 MB
ALLOWED_IMAGE_MIMES: Final[tuple[str, ...]] = ("image/jpeg", "image/png", "image/webp")

UPLOAD_PRESIGNED_EXPIRY_SECONDS: Final[int] = 300  # 5 minutes
CONFIRMATION_DEADLINE_SECONDS: Final[int] = 1200   # 20 minutes
RECOVERY_DEADLINE_SECONDS: Final[int] = 7200      # 2 hours
LEASE_DURATION_SECONDS: Final[int] = 30           # 30 seconds
MAX_CONFIRM_RETRIES: Final[int] = 5               # 5 attempts for missing staging object


# -----------------------------------------------------------------------------
# Status Workflow Validation
# -----------------------------------------------------------------------------

def can_transition(from_status: ComplaintStatus, to_status: ComplaintStatus) -> bool:
    """Check whether a transition between two statuses is structurally permitted.
    
    Forward-only:
      pending -> in-progress
      in-progress -> resolved
      resolved -> (terminal, none allowed)
    """
    return to_status in VALID_TRANSITIONS.get(from_status, ())


def get_transition_rejection_reason(from_status: ComplaintStatus, to_status: ComplaintStatus) -> str:
    """Generate a clear, human-readable reason why a transition is forbidden."""
    if from_status == to_status:
        return f"Complaint is already in '{STATUS_LABELS[from_status]}' status."
    if from_status == ComplaintStatus.RESOLVED:
        return "Resolved is a terminal state. No further status changes are allowed."
    if to_status == ComplaintStatus.PENDING:
        return "Reverting to Pending is forbidden. Status transitions are strictly forward-only."
    if from_status == ComplaintStatus.PENDING and to_status == ComplaintStatus.RESOLVED:
        return (
            "Skipping stages is forbidden. A complaint must move to 'In Progress' "
            "before it can be 'Resolved'."
        )
    return (
        f"Transition from '{STATUS_LABELS[from_status]}' to '{STATUS_LABELS[to_status]}' "
        "is not permitted by the campus workflow."
    )


def validate_status_transition(
    complaint: Complaint,
    to_status: ComplaintStatus,
    remarks: str | None = None,
) -> None:
    """Validate a requested status transition against all business rules.
    
    Raises:
        TerminalStatusError: If the complaint is already resolved.
        InvalidStatusTransitionError: If the transition skips or moves backwards.
        MissingResolutionRemarksError: If resolving without mandatory remarks.
    """
    from_status = complaint.status

    if from_status == ComplaintStatus.RESOLVED:
        raise TerminalStatusError(
            f"Complaint '{complaint.id}' is Resolved (terminal state). No further transitions allowed."
        )

    if not can_transition(from_status, to_status):
        reason = get_transition_rejection_reason(from_status, to_status)
        raise InvalidStatusTransitionError(
            f"Invalid transition for complaint '{complaint.id}': {reason}"
        )

    if to_status == ComplaintStatus.RESOLVED:
        if not remarks or not remarks.strip():
            raise MissingResolutionRemarksError(
                f"Complaint '{complaint.id}' cannot be marked Resolved without resolution remarks."
            )


# -----------------------------------------------------------------------------
# Feedback & Follow-Up Validation
# -----------------------------------------------------------------------------

def validate_feedback(
    complaint: Complaint,
    rating: Any,
    comment: str | None = None,
) -> None:
    """Validate student feedback eligibility and rating constraints.
    
    Ratings must be strictly integer instances (not booleans or floats) between 1 and 5.
    
    Raises:
        FeedbackNotAllowedError: If complaint is not resolved.
        InvalidFeedbackRatingError: If rating is not an integer or outside 1..5.
    """
    if complaint.status != ComplaintStatus.RESOLVED:
        raise FeedbackNotAllowedError(
            f"Feedback cannot be submitted for complaint '{complaint.id}' because "
            f"its status is '{complaint.status.value}'. Feedback is permitted only for Resolved complaints."
        )

    # Note: In Python, bool is a subclass of int (isinstance(True, int) is True).
    # We must explicitly reject booleans.
    if isinstance(rating, bool) or not isinstance(rating, int):
        raise InvalidFeedbackRatingError(
            f"Feedback rating must be an integer (not boolean or float), got {type(rating).__name__}."
        )

    if rating < MIN_FEEDBACK_RATING or rating > MAX_FEEDBACK_RATING:
        raise InvalidFeedbackRatingError(
            f"Feedback rating must be an integer between {MIN_FEEDBACK_RATING} and {MAX_FEEDBACK_RATING}. "
            f"Received: {rating}"
        )


def validate_follow_up_eligibility(
    parent_complaint: Complaint,
    follow_up_reason: str,
    has_active_follow_up: bool,
) -> None:
    """Validate whether a follow-up complaint can be created.
    
    Rules enforced:
    1. Parent complaint MUST be in RESOLVED status. Overdue but unresolved complaints
       (e.g. pending or in-progress) CANNOT create follow-ups under any circumstances.
    2. Follow-up explanation must contain at least 10 non-whitespace characters.
    3. No active (pending or in-progress) follow-up may already exist for this parent.

    Raises:
        FollowUpEligibilityError: If parent complaint status is not RESOLVED.
        InvalidFollowUpReasonError: If reason is too short or whitespace-only.
        ActiveFollowUpExistsError: If an active follow-up already exists.
    """
    if parent_complaint.status != ComplaintStatus.RESOLVED:
        raise FollowUpEligibilityError(
            f"Cannot create a follow-up for complaint '{parent_complaint.id}' because "
            f"its status is '{parent_complaint.status.value}'. Follow-ups are permitted only for Resolved complaints."
        )

    clean_reason = follow_up_reason.strip() if follow_up_reason else ""
    if len(clean_reason) < MIN_FOLLOW_UP_REASON_LENGTH:
        raise InvalidFollowUpReasonError(
            f"Follow-up explanation must contain at least {MIN_FOLLOW_UP_REASON_LENGTH} characters. "
            f"Received {len(clean_reason)} characters."
        )

    if has_active_follow_up:
        raise ActiveFollowUpExistsError(
            f"An active (unresolved) follow-up already exists for complaint '{parent_complaint.id}'. "
            "Only one active follow-up is permitted at a time."
        )


def validate_follow_up_title(title: str | None, parent_title: str) -> str:
    """Validate follow-up title when provided, or generate standard fallback."""
    if title is None:
        return f"[Follow-up] {parent_title.strip()}"
    return validate_text_field(title, "Follow-up title", min_length=3, max_length=150)


def toggle_upvote_on_complaint(complaint: Complaint, student_id: str) -> bool:
    """Toggle a student's upvote on a complaint.
    
    Enforces one-vote-per-student rule while allowing removal.
    
    Returns:
        True if the upvote was added, False if it was removed.
    """
    if not student_id or not student_id.strip():
        raise ValidationError("student_id must be provided to register or remove an upvote.")

    clean_id = student_id.strip()
    if clean_id in complaint.upvotes:
        complaint.upvotes.remove(clean_id)
        return False
    else:
        complaint.upvotes.append(clean_id)
        return True


# -----------------------------------------------------------------------------
# Authorization & Ownership Validation
# -----------------------------------------------------------------------------

def validate_actor_role(
    actor: Actor,
    allowed_roles: tuple[Role, ...],
    action: str,
) -> None:
    """Ensure the authenticated actor has one of the required roles.
    
    Raises:
        UnauthorizedError: If actor does not possess an authorized role.
    """
    if not isinstance(actor, Actor):
        raise UnauthorizedError(f"Authentication required: actor must be an instance of Actor, got {type(actor).__name__}.")

    if actor.role not in allowed_roles:
        allowed_str = ", ".join(r.value for r in allowed_roles)
        raise UnauthorizedError(
            f"Actor '{actor.user_id}' with role '{actor.role.value}' is not authorized to {action}. "
            f"Required role(s): {allowed_str}."
        )


def validate_complaint_ownership(
    complaint: Complaint,
    actor: Actor,
    action: str,
) -> None:
    """Ensure students can only act on their own complaints.
    
    Students are strictly restricted to tickets they authored (`submitted_by`).
    Maintenance and Admin roles can manage any ticket.
    
    Raises:
        UnauthorizedError: If a student tries to act on someone else's complaint.
    """
    if not isinstance(actor, Actor):
        raise UnauthorizedError(f"Authentication required: actor must be an instance of Actor, got {type(actor).__name__}.")

    if actor.is_student:
        if complaint.submitted_by != actor.user_id:
            raise UnauthorizedError(
                f"Student '{actor.user_id}' cannot {action} on complaint '{complaint.id}' "
                f"submitted by '{complaint.submitted_by}'."
            )


# -----------------------------------------------------------------------------
# Input Validation & SLA Overdue Enforcement
# -----------------------------------------------------------------------------

def validate_issue_category(category: Any) -> IssueCategory:
    """Validate and coerce category to IssueCategory enum.
    
    Raises:
        ValidationError: If the category is not recognized.
    """
    if isinstance(category, IssueCategory):
        return category
    if isinstance(category, str):
        cleaned = category.strip()
        for cat in IssueCategory:
            if cat.value.lower() == cleaned.lower():
                return cat
    allowed = [c.value for c in IssueCategory]
    raise ValidationError(
        f"Invalid issue category: '{category}'. Allowed categories are: {', '.join(allowed)}."
    )


def validate_text_field(
    value: str | None,
    field_name: str,
    min_length: int = 1,
    max_length: int = 1000,
) -> str:
    """Ensure a string field is non-empty after stripping and within bounds.
    
    Raises:
        ValidationError: If empty, whitespace-only, or outside length limits.
    """
    if value is None:
        raise ValidationError(f"{field_name} cannot be null or empty.")
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be a string, got {type(value).__name__}.")
    cleaned = value.strip()
    if not cleaned:
        raise ValidationError(f"{field_name} cannot be empty or whitespace-only.")
    if len(cleaned) < min_length:
        raise ValidationError(
            f"{field_name} must contain at least {min_length} characters. Received: {len(cleaned)}."
        )
    if len(cleaned) > max_length:
        raise ValidationError(
            f"{field_name} cannot exceed {max_length} characters. Received: {len(cleaned)}."
        )
    return cleaned


def validate_staff_name(staff_name: str | None) -> str | None:
    """Validate staff assignment name, rejecting empty or whitespace-only strings."""
    if staff_name is None:
        return None
    return validate_text_field(staff_name, "Assigned staff name", min_length=2, max_length=100)


def validate_overdue_threshold(
    category: IssueCategory,
    custom_threshold: Any = None,
    actor: Actor | None = None,
) -> int:
    """Calculate and validate overdue threshold in days.
    
    SLA Rules:
    - Fire Safety has a strict, non-negotiable 3-day SLA.
      All custom overrides are rejected regardless of caller role.
    - Other categories default to 7 days, or an integer between 1 and 30 days.
    - Thresholds must be actual integers (not booleans, floats, or malformed strings).
    
    Raises:
        ValidationError: If an invalid override is attempted.
    """
    if category == IssueCategory.FIRE_SAFETY:
        if custom_threshold is not None and custom_threshold != FIRE_SAFETY_SLA_DAYS:
            raise ValidationError(
                f"Fire Safety complaints have a non-negotiable {FIRE_SAFETY_SLA_DAYS}-day SLA. "
                f"Custom override of {custom_threshold} days is rejected."
            )
        return FIRE_SAFETY_SLA_DAYS

    if custom_threshold is None:
        return DEFAULT_OVERDUE_DAYS

    # Reject booleans (subclass of int) and non-ints
    if isinstance(custom_threshold, bool) or not isinstance(custom_threshold, int):
        raise ValidationError(
            f"Overdue threshold must be an integer between {MIN_OVERDUE_DAYS} and {MAX_OVERDUE_DAYS}, "
            f"got {type(custom_threshold).__name__}."
        )

    if custom_threshold < MIN_OVERDUE_DAYS or custom_threshold > MAX_OVERDUE_DAYS:
        raise ValidationError(
            f"Overdue threshold must be between {MIN_OVERDUE_DAYS} and {MAX_OVERDUE_DAYS} days. "
            f"Received: {custom_threshold}."
        )
    return custom_threshold


def detect_image_mime(decoded: bytes) -> str | None:
    """Detect image format MIME from binary magic byte signatures.
    
    Permitted formats:
    - JPEG: starts with b"\\xff\\xd8\\xff"
    - PNG: starts with b"\\x89PNG\\r\\n\\x1a\\n"
    - WEBP: starts with b"RIFF" and has b"WEBP" at bytes 8..12
    """
    if decoded.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if decoded.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(decoded) >= 12 and decoded.startswith(b"RIFF") and decoded[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate_image_base64(image_str: str | None) -> str | None:
    """Validate image attachment format, size, and base64 decodability.
    
    Framework-independent: uses only standard library `base64` and `re`.
    
    Constraints:
    - Maximum size: 5 MB (decoded bytes)
    - Permitted formats: JPEG, PNG, WEBP
    - Verifies binary signature matches declared MIME type and allowed image formats.
    - Rejects empty, un-decodable, corrupted, mismatched, or oversized image data.
    
    Returns:
        Cleaned image string if provided, or None.
    Raises:
        ValidationError: If image data is invalid, unparseable, unsupported, mismatched, or exceeds 5 MB.
    """
    if image_str is None:
        return None
    if not isinstance(image_str, str):
        raise ValidationError(f"Image attachment must be a string, got {type(image_str).__name__}.")

    cleaned = image_str.strip()
    if not cleaned:
        return None

    declared_mime = None
    raw_b64 = cleaned

    # Check for data URI scheme: data:image/jpeg;base64,...
    if cleaned.startswith("data:"):
        match = re.match(r"^data:(image/[a-zA-Z0-9\+\-\.]+);base64,(.+)$", cleaned, re.DOTALL)
        if not match:
            raise ValidationError("Invalid image data URI format. Expected 'data:<mime>;base64,<payload>'.")
        declared_mime = match.group(1).lower()
        if declared_mime == "image/jpg":
            declared_mime = "image/jpeg"
        if declared_mime not in ALLOWED_IMAGE_MIMES:
            raise ValidationError(
                f"Unsupported image format: '{declared_mime}'. Permitted formats: {', '.join(ALLOWED_IMAGE_MIMES)}."
            )
        raw_b64 = match.group(2).strip()

    # Decode base64 to verify integrity
    try:
        decoded = base64.b64decode(raw_b64, validate=True)
    except (binascii.Error, ValueError) as err:
        raise ValidationError(f"Invalid base64 encoding in image attachment: {err}")

    if len(decoded) == 0:
        raise ValidationError("Image attachment payload cannot be empty.")

    if len(decoded) > MAX_IMAGE_BYTES:
        raise ValidationError(
            f"Image attachment size ({len(decoded)} bytes) exceeds the maximum allowed limit of {MAX_IMAGE_BYTES} bytes (5 MB)."
        )

    # Verify signature against allowed formats
    detected_mime = detect_image_mime(decoded)
    if detected_mime is None:
        raise ValidationError(
            "Unsupported image content signature. Decoded bytes do not match JPEG, PNG, or WEBP."
        )

    # Cross-check declared MIME against actual bytes
    if declared_mime is not None and detected_mime != declared_mime:
        raise ValidationError(
            f"Image content signature ({detected_mime}) does not match declared MIME type '{declared_mime}'."
        )

    return cleaned
