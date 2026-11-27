"""Complaint application service for MAIT CampusFix.

Orchestrates domain operations, enforces workflow rules, and mediates
between callers and storage repositories with strict server-side authorization
and optimistic concurrency control.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence

from campusfix.domain.models import (
    Actor,
    Complaint,
    ComplaintFeedback,
    ComplaintStatus,
    IssueCategory,
    Role,
    StatusHistoryEntry,
)
from campusfix.domain.rules import (
    CampusFixDomainError,
    ComplaintAlreadyExistsError,
    ComplaintNotFoundError,
    OptimisticLockError,
    TerminalStatusError,
    UnauthorizedError,
    ValidationError,
    validate_actor_role,
    validate_complaint_ownership,
    validate_feedback,
    validate_follow_up_eligibility,
    validate_follow_up_title,
    validate_image_base64,
    validate_issue_category,
    validate_overdue_threshold,
    validate_staff_name,
    validate_status_transition,
    validate_text_field,
)
from campusfix.repositories.interfaces import ComplaintRepository
from .id_generator import IdGenerator, UUIDYearIdGenerator


def _require_actor(actor: Any) -> Actor:
    """Ensure the provided actor is a verified, authentic Actor instance.
    
    Security rule:
    Strings, request bodies, or non-Actor objects must NEVER be coerced
    into authenticated principals.
    """
    if not isinstance(actor, Actor):
        raise UnauthorizedError(
            f"Authentication failed: operation requires an authenticated Actor instance, "
            f"got {type(actor).__name__}."
        )
    return actor


class ComplaintService:
    """Service handling all complaint lifecycle, accountability, and permission use cases.
    
    Security Architecture & Production Guidelines:
    ------------------------------------------------
    In production with AWS Cognito + API Gateway:
    - The API adapter layer (e.g. AWS Lambda handler) MUST construct the `Actor` instance
      from cryptographically verified Cognito JWT claims:
      * `user_id`: from `sub` claim.
      * `role`: mapped from verified Cognito User Pool Groups (`cognito:groups` -> STUDENT, MAINTENANCE, ADMIN).
    - Client requests must NEVER be allowed to supply their own role or spoof user_id.
    - All business methods require an explicit `Actor` instance and enforce server-side
      permissions and ticket ownership.
    """

    def __init__(
        self,
        repository: ComplaintRepository,
        id_generator: IdGenerator | None = None,
    ) -> None:
        self._repository = repository
        self._id_generator = id_generator or UUIDYearIdGenerator()

    # -------------------------------------------------------------------------
    # Core Queries
    # -------------------------------------------------------------------------

    def get_complaint(self, complaint_id: str) -> Complaint | None:
        """Fetch a complaint by ID."""
        return self._repository.get(complaint_id)

    def get_complaint_or_raise(self, complaint_id: str) -> Complaint:
        """Fetch a complaint by ID or raise ComplaintNotFoundError."""
        complaint = self._repository.get(complaint_id)
        if complaint is None:
            raise ComplaintNotFoundError(f"Complaint '{complaint_id}' was not found.")
        return complaint

    def list_complaints(
        self,
        status: ComplaintStatus | None = None,
        building: str | None = None,
        category: IssueCategory | None = None,
    ) -> list[Complaint]:
        """List complaints with optional filters."""
        return self._repository.list_all(status=status, building=building, category=category)

    def list_complaints_paginated(
        self,
        status: ComplaintStatus | None = None,
        building: str | None = None,
        category: IssueCategory | None = None,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[Complaint], str | None]:
        """List complaints with indexed filters (status, building, category) and cursor pagination.
        
        Implementation Note:
        - Building filtering is fully supported alongside status and category.
        - In-memory pagination uses an unsigned, unencrypted base64 array offset for local testing.
        - It is not a secure production cursor and is not safe against concurrent insertions.
        - Production HMAC-SHA256 signed DynamoDB ExclusiveStartKey cursor support is deferred to Step 2.
        """
        return self._repository.list_paginated(
            status=status,
            building=building,
            category=category,
            limit=limit,
            cursor=cursor,
        )

    def get_linked_follow_ups(self, original_complaint_id: str) -> list[Complaint]:
        """List all follow-up complaints originating from a parent complaint."""
        return self._repository.find_by_original_id(original_complaint_id)

    # -------------------------------------------------------------------------
    # Creation
    # -------------------------------------------------------------------------

    def create_complaint(
        self,
        title: str,
        description: str,
        building: str,
        floor: str,
        room_number: str,
        category: IssueCategory | str,
        actor: Actor,
        image_base64: str | None = None,
        overdue_threshold_days: int | None = None,
        *,
        submitted_by: str | None = None,
    ) -> Complaint:
        """Create a new original complaint.
        
        Enforces:
        - Permission: Requires Actor with Role.STUDENT
        - Identity: Always derived from authenticated Actor.user_id
        - Input validation: Non-empty trimmed fields, min length limits
        - Category validation: Valid IssueCategory enum member
        - Non-negotiable Fire Safety SLA: 3-day SLA cannot be overridden by students
        - Image validation: Valid format (JPEG, PNG, WEBP), valid base64, size <= 5 MB
        - Default status: PENDING
        - Atomicity: Stored via repository `create()` preventing ID overwrite
        """
        verified_actor = _require_actor(actor)
        validate_actor_role(verified_actor, (Role.STUDENT,), "create a complaint")

        if submitted_by is not None and submitted_by.strip() != verified_actor.user_id:
            raise UnauthorizedError(
                f"Cannot create complaint for identity '{submitted_by}': "
                f"does not match authenticated student '{verified_actor.user_id}'."
            )

        # Field validations
        clean_title = validate_text_field(title, "Complaint title", min_length=3, max_length=150)
        clean_desc = validate_text_field(description, "Complaint description", min_length=20, max_length=3000)
        clean_building = validate_text_field(building, "Building", min_length=1, max_length=50)
        clean_floor = validate_text_field(floor, "Floor", min_length=1, max_length=30)
        clean_room = validate_text_field(room_number, "Room number", min_length=1, max_length=30)
        cat_enum = validate_issue_category(category)

        # Image attachment validation (framework-independent, max 5 MB)
        clean_image = validate_image_base64(image_base64)

        # Enforce SLA & Overdue Threshold (including Fire Safety protection)
        threshold = validate_overdue_threshold(cat_enum, overdue_threshold_days, verified_actor)

        complaint_id = self._id_generator.next_id()
        now = datetime.now(timezone.utc)

        complaint = Complaint(
            id=complaint_id,
            title=clean_title,
            description=clean_desc,
            building=clean_building,
            floor=clean_floor,
            room_number=clean_room,
            category=cat_enum,
            status=ComplaintStatus.PENDING,
            submitted_at=now,
            submitted_by=verified_actor.user_id,
            overdue_threshold_days=threshold,
            image_base64=clean_image,
            upvotes=[],
            status_history=[],
            is_follow_up=False,
            version=1,
        )

        self._repository.create(complaint)
        return complaint

    # -------------------------------------------------------------------------
    # Status Workflow
    # -------------------------------------------------------------------------

    def transition_status(
        self,
        complaint_id: str,
        to_status: ComplaintStatus | str,
        actor: Actor | None = None,
        remarks: str | None = None,
        assigned_staff: str | None = None,
        expected_version: int | None = None,
        *,
        actor_id: Actor | None = None,
    ) -> Complaint:
        """Transition a complaint's status forward in the lifecycle.
        
        Strict forward-only rule:
            PENDING -> IN_PROGRESS -> RESOLVED (terminal)
        
        Enforces:
        - Permission: Requires Actor with Role.MAINTENANCE or Role.ADMIN
        - Rejection of backward transitions or stage-skipping
        - Rejection of transitions from RESOLVED
        - Mandatory resolution remarks before marking RESOLVED
        - Automatic audit log entry in status_history
        - Setting resolved_at timestamp when entering RESOLVED
        - Optimistic Concurrency Control: Preserves expected_version to reject concurrent edits
        """
        effective_actor = actor if actor is not None else actor_id
        verified_actor = _require_actor(effective_actor)

        if isinstance(to_status, ComplaintStatus):
            status_enum = to_status
        elif isinstance(to_status, str):
            try:
                status_enum = ComplaintStatus(to_status)
            except ValueError:
                valid_values = [s.value for s in ComplaintStatus]
                raise ValidationError(
                    f"Invalid status '{to_status}'. Valid statuses are: {', '.join(valid_values)}."
                )
        else:
            raise ValidationError(f"Invalid status type: {type(to_status).__name__}.")
        validate_actor_role(
            verified_actor,
            (Role.MAINTENANCE, Role.ADMIN),
            f"transition status to '{status_enum.value}'",
        )

        complaint = self.get_complaint_or_raise(complaint_id)
        if expected_version is not None and complaint.version != expected_version:
            raise OptimisticLockError(
                f"Version conflict transitioning complaint '{complaint_id}': "
                f"expected version {expected_version}, but found {complaint.version}."
            )
        target_version = expected_version if expected_version is not None else complaint.version
        from_status = complaint.status

        # Validate transition against business rules (including terminal state check)
        validate_status_transition(complaint, status_enum, remarks)

        now = datetime.now(timezone.utc)
        clean_remarks = remarks.strip() if remarks else None
        clean_staff = validate_staff_name(assigned_staff) if assigned_staff is not None else None

        # Record immutable audit entry
        history_entry = StatusHistoryEntry(
            from_status=from_status,
            to_status=status_enum,
            changed_at=now,
            changed_by=verified_actor.user_id,
            remarks=clean_remarks,
        )
        complaint.status_history.append(history_entry)

        # Apply state updates
        complaint.status = status_enum
        if assigned_staff is not None:
            complaint.assigned_staff = clean_staff

        if status_enum == ComplaintStatus.RESOLVED:
            complaint.resolved_at = now
            complaint.resolution_remarks = clean_remarks

        # Optimistic update: fails if another transaction updated the complaint in the meantime
        self._repository.update(complaint, expected_version=target_version)
        return complaint

    # -------------------------------------------------------------------------
    # Post-Resolution Feedback
    # -------------------------------------------------------------------------

    def submit_feedback(
        self,
        complaint_id: str,
        rating: int,
        actor: Actor | None = None,
        comment: str | None = None,
        expected_version: int | None = None,
        *,
        actor_id: Actor | None = None,
    ) -> Complaint:
        """Submit or edit student satisfaction feedback for a resolved complaint.
        
        Enforces:
        - Permission: Requires Actor with Role.STUDENT
        - Ownership: Student can only submit feedback for their OWN complaint
        - Complaint must be in RESOLVED status
        - Rating must be integer between 1 and 5 (booleans rejected)
        - Preserves initial submitted_at timestamp when updating existing feedback
        - Optimistic Concurrency Control: Verifies expected_version during update
        """
        effective_actor = actor if actor is not None else actor_id
        verified_actor = _require_actor(effective_actor)
        validate_actor_role(verified_actor, (Role.STUDENT,), "submit feedback")

        complaint = self.get_complaint_or_raise(complaint_id)
        if expected_version is not None and complaint.version != expected_version:
            raise OptimisticLockError(
                f"Version conflict submitting feedback for complaint '{complaint_id}': "
                f"expected version {expected_version}, but found {complaint.version}."
            )
        target_version = expected_version if expected_version is not None else complaint.version

        validate_complaint_ownership(complaint, verified_actor, "submit feedback")
        validate_feedback(complaint, rating, comment)

        now = datetime.now(timezone.utc)
        clean_comment = comment.strip() if comment else None

        if complaint.feedback is not None:
            # Edit existing feedback
            complaint.feedback.rating = rating
            complaint.feedback.comment = clean_comment
            complaint.feedback.updated_at = now
        else:
            # First-time submission
            complaint.feedback = ComplaintFeedback(
                rating=rating,
                comment=clean_comment,
                submitted_at=now,
            )

        self._repository.update(complaint, expected_version=target_version)
        return complaint

    # -------------------------------------------------------------------------
    # Follow-Up Complaints
    # -------------------------------------------------------------------------

    def create_follow_up(
        self,
        original_complaint_id: str,
        follow_up_reason: str,
        actor: Actor | None = None,
        title: str | None = None,
        image_base64: str | None = None,
        *,
        actor_id: Actor | None = None,
    ) -> Complaint:
        """Create an independent follow-up ticket linked to a resolved parent.
        
        Enforces:
        - Permission: Requires Actor with Role.STUDENT
        - Ownership: Student can only create follow-ups for their OWN complaint
        - Original parent complaint MUST be in RESOLVED status.
          Overdue but unresolved complaints (e.g. pending or in-progress) CANNOT create follow-ups.
        - Follow-up explanation MUST be at least 10 non-whitespace characters
        - Validates follow-up title when provided
        - Image attachment validated (JPEG, PNG, WEBP, <= 5 MB)
        - Concurrency protection: Atomic check-and-insert in repository guarantees
          only one active follow-up can ever be created per parent ticket
        - Generates a new unique full-UUID collision-resistant ID
        - Original parent ticket remains permanently RESOLVED
        - Follow-up begins in PENDING status with independent lifecycle
        - Does NOT inherit parent's upvotes or feedback
        """
        effective_actor = actor if actor is not None else actor_id
        verified_actor = _require_actor(effective_actor)
        validate_actor_role(verified_actor, (Role.STUDENT,), "create a follow-up complaint")

        parent = self.get_complaint_or_raise(original_complaint_id)
        validate_complaint_ownership(parent, verified_actor, "create a follow-up")

        # Validate eligibility without checking has_active yet (atomic repo check handles active follow-up)
        validate_follow_up_eligibility(parent, follow_up_reason, has_active_follow_up=False)

        clean_reason = validate_text_field(follow_up_reason, "Follow-up explanation", min_length=10, max_length=2000)
        clean_title = validate_follow_up_title(title, parent.title)
        clean_image = validate_image_base64(image_base64)

        follow_up_id = self._id_generator.next_id()
        now = datetime.now(timezone.utc)

        follow_up = Complaint(
            id=follow_up_id,
            title=clean_title,
            description=parent.description,
            building=parent.building,
            floor=parent.floor,
            room_number=parent.room_number,
            category=parent.category,
            status=ComplaintStatus.PENDING,
            submitted_at=now,
            submitted_by=verified_actor.user_id,
            overdue_threshold_days=parent.overdue_threshold_days,
            image_base64=clean_image,
            upvotes=[],
            status_history=[],
            feedback=None,
            is_follow_up=True,
            original_complaint_id=parent.id,
            follow_up_reason=clean_reason,
            version=1,
        )

        # Atomic persistence verifies parent status, active follow-up absence, and saves under lock
        return self._repository.create_follow_up_atomic(parent.id, follow_up)

    # -------------------------------------------------------------------------
    # Upvotes
    # -------------------------------------------------------------------------

    def toggle_upvote(
        self,
        complaint_id: str,
        actor: Actor | None = None,
        *,
        student_id: Actor | None = None,
    ) -> tuple[Complaint, bool]:
        """Toggle an upvote for a student on a complaint.
        
        Enforces:
        - Permission: Requires Actor with Role.STUDENT
        - 1 upvote per student per complaint (adds if absent, removes if present)
        - Atomic execution in repository eliminates lost-update race conditions
        
        Returns:
            Tuple of (updated_complaint, upvote_added: bool)
        """
        effective_actor = actor if actor is not None else student_id
        verified_actor = _require_actor(effective_actor)
        validate_actor_role(verified_actor, (Role.STUDENT,), "upvote complaints")

        return self._repository.toggle_upvote_atomic(complaint_id, verified_actor.user_id)

    def add_upvote(
        self,
        complaint_id: str,
        actor: Actor | None = None,
        *,
        student_id: Actor | None = None,
    ) -> tuple[Complaint, bool]:
        """Atomically register a student's upvote (Option B - Idempotent PUT).
        
        Enforces:
        - Permission: Requires Actor with Role.STUDENT
        - Idempotency: Multiple calls leave the complaint upvoted and do not increment version
        
        Returns:
            Tuple of (updated_complaint, changed: bool)
        """
        effective_actor = actor if actor is not None else student_id
        verified_actor = _require_actor(effective_actor)
        validate_actor_role(verified_actor, (Role.STUDENT,), "upvote complaints")

        return self._repository.add_upvote_atomic(complaint_id, verified_actor.user_id)

    def remove_upvote(
        self,
        complaint_id: str,
        actor: Actor | None = None,
        *,
        student_id: Actor | None = None,
    ) -> tuple[Complaint, bool]:
        """Atomically withdraw a student's upvote (Option B - Idempotent DELETE).
        
        Enforces:
        - Permission: Requires Actor with Role.STUDENT
        - Idempotency: Multiple calls leave the complaint not upvoted and do not increment version
        
        Returns:
            Tuple of (updated_complaint, changed: bool)
        """
        effective_actor = actor if actor is not None else student_id
        verified_actor = _require_actor(effective_actor)
        validate_actor_role(verified_actor, (Role.STUDENT,), "remove upvote from complaints")

        return self._repository.remove_upvote_atomic(complaint_id, verified_actor.user_id)

    # -------------------------------------------------------------------------
    # Staff Assignment
    # -------------------------------------------------------------------------

    def assign_staff(
        self,
        complaint_id: str,
        staff_name: str,
        actor: Actor | None = None,
        expected_version: int | None = None,
        *,
        actor_id: Actor | None = None,
    ) -> Complaint:
        """Assign or reassign responsible maintenance staff to a complaint.
        
        Enforces:
        - Permission: Requires Actor with Role.MAINTENANCE or Role.ADMIN
        - Input validation: Staff name cannot be empty or whitespace-only
        - Optimistic Concurrency Control: Checks expected_version
        """
        effective_actor = actor if actor is not None else actor_id
        verified_actor = _require_actor(effective_actor)
        validate_actor_role(verified_actor, (Role.MAINTENANCE, Role.ADMIN), "assign staff")

        complaint = self.get_complaint_or_raise(complaint_id)
        if expected_version is not None and complaint.version != expected_version:
            raise OptimisticLockError(
                f"Version conflict assigning staff to complaint '{complaint_id}': "
                f"expected version {expected_version}, but found {complaint.version}."
            )
        target_version = expected_version if expected_version is not None else complaint.version

        clean_staff = validate_staff_name(staff_name)
        complaint.assigned_staff = clean_staff
        self._repository.update(complaint, expected_version=target_version)
        return complaint
