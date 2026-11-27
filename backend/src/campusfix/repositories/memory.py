"""In-memory implementation of the ComplaintRepository.

Ideal for local testing, unit tests, and development without database dependencies.
Equipped with threading locks for thread-safe operations in multi-threaded test/dev servers.
"""

from __future__ import annotations

import base64
import copy
from datetime import datetime, timedelta
import threading
from typing import Dict

from campusfix.domain.models import (
    AtomicAttachmentCommitResult,
    Complaint,
    ComplaintStatus,
    IssueCategory,
    TagOutboxRecord,
    UploadAttempt,
    UploadAttemptStatus,
    UploadIntent,
    UploadIntentStatus,
    ValidationError,
    normalize_sha256_hex,
    parse_utc_iso_datetime,
)
from campusfix.domain.rules import (
    ActiveFollowUpExistsError,
    ComplaintAlreadyExistsError,
    ComplaintNotFoundError,
    FollowUpEligibilityError,
    ImmutableFieldViolationError,
    InvalidFollowUpReasonError,
    OptimisticLockError,
    StaleLeaseError,
    UnauthorizedError,
    UploadFailedError,
    UploadIntentExpiredError,
    UploadIntentNotFoundError,
)
from .interfaces import ComplaintRepository, UploadIntentRepository


class InMemoryComplaintRepository(ComplaintRepository, UploadIntentRepository):
    """Thread-safe, in-memory repository for storing complaints and upload intents in dictionaries."""

    def __init__(self, initial_complaints: list[Complaint] | None = None) -> None:
        self._storage: Dict[str, Complaint] = {}
        self._intents: Dict[tuple[str, str], UploadIntent] = {}
        # Stage B2: attempt records keyed by (complaint_id, intent_id, lease_token)
        self._attempts: Dict[tuple[str, str, str], UploadAttempt] = {}
        # Stage B2: outbox records keyed by intent_id
        self._outbox: Dict[str, TagOutboxRecord] = {}
        self._lock = threading.RLock()
        if initial_complaints:
            for complaint in initial_complaints:
                self.seed(complaint)

    def get(self, complaint_id: str) -> Complaint | None:
        """Retrieve a clone of the complaint by ID."""
        with self._lock:
            complaint = self._storage.get(complaint_id)
            if complaint is None:
                return None
            return copy.deepcopy(complaint)

    def create(self, complaint: Complaint) -> None:
        """Persist a new complaint, raising an error if the ID already exists."""
        with self._lock:
            if complaint.id in self._storage:
                raise ComplaintAlreadyExistsError(
                    f"Complaint with ID '{complaint.id}' already exists in storage."
                )
            self._storage[complaint.id] = copy.deepcopy(complaint)

    def update(self, complaint: Complaint, expected_version: int) -> None:
        """Update an existing complaint in storage under optimistic concurrency control.
        
        Args:
            complaint: The updated complaint aggregate.
            expected_version: The mandatory positive integer version observed when the caller loaded the complaint.
        
        Raises:
            ValidationError: If expected_version is None, boolean, non-integer, or < 1.
            ComplaintNotFoundError: If target complaint ID is missing.
            OptimisticLockError: If stored version does not match expected_version.
        """
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 1:
            raise ValidationError(
                f"expected_version must be a mandatory positive integer >= 1 (not boolean or None). "
                f"Received: {expected_version!r} (type: {type(expected_version).__name__})"
            )

        with self._lock:
            stored = self._storage.get(complaint.id)
            if stored is None:
                raise ComplaintNotFoundError(
                    f"Complaint with ID '{complaint.id}' was not found for update."
                )

            if stored.version != expected_version:
                raise OptimisticLockError(
                    f"Version conflict updating complaint '{complaint.id}': "
                    f"expected version {expected_version}, but found {stored.version}."
                )

            # Increment stored version exactly once on successful update
            new_version = stored.version + 1
            complaint.version = new_version
            self._storage[complaint.id] = copy.deepcopy(complaint)

    def seed(self, complaint: Complaint) -> None:
        """Persist a complaint for initial data fixtures or test seeding.

        Raises:
            ComplaintAlreadyExistsError: If a complaint with this ID already exists.
                Duplicate seeding indicates a test-setup error and is rejected explicitly.
        """
        with self._lock:
            if complaint.id in self._storage:
                raise ComplaintAlreadyExistsError(
                    f"seed() rejected duplicate ID '{complaint.id}'. "
                    "Each fixture ID must be unique. Use clear() before re-seeding a repository."
                )
            self._storage[complaint.id] = copy.deepcopy(complaint)

    def save(self, complaint: Complaint) -> None:
        """Deprecated unrestricted save. Overwriting existing complaints is forbidden."""
        with self._lock:
            if complaint.id in self._storage:
                raise ValidationError(
                    f"Unrestricted overwrite of existing complaint '{complaint.id}' via save() is forbidden. "
                    "Use update() with mandatory expected_version."
                )
            self._storage[complaint.id] = copy.deepcopy(complaint)

    def create_follow_up_atomic(
        self,
        original_complaint_id: str,
        follow_up: Complaint,
    ) -> Complaint:
        """Atomically check all parent and child follow-up invariants and persist the follow-up under lock.
        
        All checks and insertion execute within the same lock acquisition.
        """
        with self._lock:
            parent = self._storage.get(original_complaint_id)
            if parent is None:
                raise ComplaintNotFoundError(
                    f"Original complaint '{original_complaint_id}' was not found."
                )

            # 1. Verify parent is currently Resolved (overdue but unresolved complaints are strictly ineligible)
            if parent.status != ComplaintStatus.RESOLVED:
                raise FollowUpEligibilityError(
                    f"Cannot create a follow-up for complaint '{original_complaint_id}' because "
                    f"its status is '{parent.status.value}'. Follow-ups are permitted only for Resolved complaints."
                )

            # 2. Child ID differs from parent ID
            if follow_up.id == original_complaint_id:
                raise ValidationError(
                    f"Follow-up child ID '{follow_up.id}' cannot be identical to parent ID."
                )

            # 3. Child ID is unique
            if follow_up.id in self._storage:
                raise ComplaintAlreadyExistsError(
                    f"Follow-up complaint with ID '{follow_up.id}' already exists."
                )

            # 4. Child has is_follow_up=True
            if not isinstance(follow_up.is_follow_up, bool) or not follow_up.is_follow_up:
                raise ValidationError("Follow-up complaint must have is_follow_up=True.")

            # 5. Child original_complaint_id matches supplied parent ID
            if follow_up.original_complaint_id != original_complaint_id:
                raise ValidationError(
                    f"Follow-up original_complaint_id ('{follow_up.original_complaint_id}') must match parent ID ('{original_complaint_id}')."
                )

            # 6. Child starts in PENDING status
            if follow_up.status != ComplaintStatus.PENDING:
                raise ValidationError(
                    f"Follow-up complaint must start in 'pending' status, got '{follow_up.status.value}'."
                )

            # 7. Child has a valid follow-up reason (>= 10 chars)
            if (
                not follow_up.follow_up_reason
                or not isinstance(follow_up.follow_up_reason, str)
                or len(follow_up.follow_up_reason.strip()) < 10
            ):
                raise InvalidFollowUpReasonError(
                    "Follow-up complaint must have a valid follow-up reason of at least 10 characters."
                )

            # 8. Child submitter matches original complaint owner
            if follow_up.submitted_by != parent.submitted_by:
                raise UnauthorizedError(
                    f"Follow-up submitter '{follow_up.submitted_by}' does not match original complaint owner '{parent.submitted_by}'."
                )

            # 9. Child does not inherit parent upvotes or feedback
            if follow_up.upvotes and len(follow_up.upvotes) > 0:
                raise ValidationError("Follow-up complaint must start with zero upvotes.")
            if follow_up.feedback is not None:
                raise ValidationError("Follow-up complaint cannot inherit or contain feedback upon creation.")

            # 10. Check if any existing follow-up for this parent is active (not resolved)
            for c in self._storage.values():
                if (
                    c.original_complaint_id == original_complaint_id
                    and c.status != ComplaintStatus.RESOLVED
                ):
                    raise ActiveFollowUpExistsError(
                        f"An active (unresolved) follow-up already exists for complaint '{original_complaint_id}'."
                    )

            # 11. Atomic insertion
            self._storage[follow_up.id] = copy.deepcopy(follow_up)
            return copy.deepcopy(follow_up)

    def toggle_upvote_atomic(
        self,
        complaint_id: str,
        student_id: str,
    ) -> tuple[Complaint, bool]:
        """Atomically toggle a student's upvote, eliminating lost-update race conditions."""
        with self._lock:
            complaint = self._storage.get(complaint_id)
            if complaint is None:
                raise ComplaintNotFoundError(
                    f"Complaint '{complaint_id}' was not found for upvote."
                )

            clean_id = student_id.strip()
            if clean_id in complaint.upvotes:
                complaint.upvotes.remove(clean_id)
                added = False
            else:
                complaint.upvotes.append(clean_id)
                added = True

            complaint.version += 1
            return copy.deepcopy(complaint), added

    def add_upvote_atomic(
        self,
        complaint_id: str,
        student_id: str,
    ) -> tuple[Complaint, bool]:
        """Atomically register a student's upvote (Option B - Idempotent)."""
        with self._lock:
            complaint = self._storage.get(complaint_id)
            if complaint is None:
                raise ComplaintNotFoundError(
                    f"Complaint '{complaint_id}' was not found for upvote."
                )

            clean_id = student_id.strip()
            if clean_id in complaint.upvotes:
                return copy.deepcopy(complaint), False

            complaint.upvotes.append(clean_id)
            complaint.version += 1
            return copy.deepcopy(complaint), True

    def remove_upvote_atomic(
        self,
        complaint_id: str,
        student_id: str,
    ) -> tuple[Complaint, bool]:
        """Atomically withdraw a student's upvote (Option B - Idempotent)."""
        with self._lock:
            complaint = self._storage.get(complaint_id)
            if complaint is None:
                raise ComplaintNotFoundError(
                    f"Complaint '{complaint_id}' was not found for upvote."
                )

            clean_id = student_id.strip()
            if clean_id not in complaint.upvotes:
                return copy.deepcopy(complaint), False

            complaint.upvotes.remove(clean_id)
            complaint.version += 1
            return copy.deepcopy(complaint), True

    def list_all(
        self,
        status: ComplaintStatus | None = None,
        building: str | None = None,
        category: IssueCategory | None = None,
    ) -> list[Complaint]:
        """List all complaints matching the optional filters."""
        with self._lock:
            results: list[Complaint] = []
            for c in self._storage.values():
                if status is not None and c.status != status:
                    continue
                if building is not None and c.building.lower() != building.lower():
                    continue
                if category is not None and c.category != category:
                    continue
                results.append(copy.deepcopy(c))

            # Deterministic ordering: newest submission first, tie-breaking by ID descending
            results.sort(key=lambda x: (x.submitted_at, x.id), reverse=True)
            return results

    def list_paginated(
        self,
        status: ComplaintStatus | None = None,
        building: str | None = None,
        category: IssueCategory | None = None,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[Complaint], str | None]:
        """List complaints with indexed filtering and cursor-based pagination.
        
        Implementation Note:
        - In-memory pagination uses a deterministic compound sort (submitted_at, id) descending
          and an unsigned base64-encoded integer array offset.
        - This cursor is strictly a test double for local development and contract verification.
        - It does NOT provide cross-request insertion immunity or cryptographic tamper-proofing.
        - Production HMAC-SHA256 signed cursor tokens with KMS verification and
          DynamoDB ExclusiveStartKey pagination are deferred to Step 2.
        """
        if limit <= 0 or limit > 100:
            raise ValidationError(f"Pagination limit must be between 1 and 100, got {limit}.")

        all_items = self.list_all(status=status, building=building, category=category)
        start_idx = 0
        if cursor:
            try:
                decoded = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
                start_idx = int(decoded)
            except Exception as err:
                raise ValidationError(f"Invalid pagination cursor: {cursor!r}") from err

        page_items = all_items[start_idx : start_idx + limit]
        next_idx = start_idx + len(page_items)
        next_cursor = None
        if next_idx < len(all_items):
            next_cursor = base64.urlsafe_b64encode(str(next_idx).encode("utf-8")).decode("ascii")
        return page_items, next_cursor

    def find_by_original_id(self, original_complaint_id: str) -> list[Complaint]:
        """Retrieve all follow-ups linked to the given original complaint ID."""
        with self._lock:
            matches = [
                copy.deepcopy(c)
                for c in self._storage.values()
                if c.original_complaint_id == original_complaint_id
            ]
            matches.sort(key=lambda x: x.submitted_at)
            return matches

    def has_active_follow_up(self, original_complaint_id: str) -> bool:
        """Return True if any follow-up for original_complaint_id is NOT resolved."""
        with self._lock:
            for c in self._storage.values():
                if (
                    c.original_complaint_id == original_complaint_id
                    and c.status != ComplaintStatus.RESOLVED
                ):
                    return True
            return False

    def count(self) -> int:
        """Return total complaint count."""
        with self._lock:
            return len(self._storage)

    # -------------------------------------------------------------------------
    # UploadIntentRepository implementation
    # -------------------------------------------------------------------------

    def create_intent(self, intent: UploadIntent) -> None:
        """Persist a new upload intent."""
        with self._lock:
            if intent.complaint_id not in self._storage:
                raise ComplaintNotFoundError(
                    f"Complaint '{intent.complaint_id}' was not found for upload intent."
                )
            key = (intent.complaint_id, intent.intent_id)
            self._intents[key] = copy.deepcopy(intent)

    def get_intent(self, complaint_id: str, intent_id: str) -> UploadIntent | None:
        """Retrieve an upload intent."""
        with self._lock:
            intent = self._intents.get((complaint_id, intent_id))
            if intent is None:
                return None
            return copy.deepcopy(intent)

    def claim_intent_lease(
        self,
        complaint_id: str,
        intent_id: str,
        lease_token: str,
        lease_duration_seconds: int,
        now: datetime,
    ) -> UploadIntent:
        """Atomically claim or takeover the lease on an upload intent."""
        with self._lock:
            key = (complaint_id, intent_id)
            intent = self._intents.get(key)
            if intent is None:
                raise UploadIntentNotFoundError(
                    f"Upload intent '{intent_id}' for complaint '{complaint_id}' was not found."
                )

            if intent.status == UploadIntentStatus.VERIFIED:
                return copy.deepcopy(intent)

            if intent.status == UploadIntentStatus.FAILED:
                raise UploadFailedError(
                    f"Upload intent '{intent_id}' has already failed: {intent.failure_reason or 'unknown reason'}."
                )

            if intent.status == UploadIntentStatus.PENDING_UPLOAD:
                if now > intent.confirmation_deadline:
                    intent.status = UploadIntentStatus.FAILED
                    intent.failure_reason = "Confirmation deadline expired"
                    intent.completed_at = now
                    raise UploadIntentExpiredError(
                        f"Upload intent '{intent_id}' confirmation deadline expired at {intent.confirmation_deadline.isoformat()}."
                    )
            elif intent.status == UploadIntentStatus.CONFIRMING:
                if now > intent.recovery_deadline:
                    intent.status = UploadIntentStatus.FAILED
                    intent.failure_reason = "Recovery deadline expired"
                    intent.completed_at = now
                    raise UploadIntentExpiredError(
                        f"Upload intent '{intent_id}' recovery deadline expired at {intent.recovery_deadline.isoformat()}."
                    )
                # Check active lease
                if intent.lease_expires_at is not None and now <= intent.lease_expires_at:
                    if intent.lease_token != lease_token:
                        raise StaleLeaseError(
                            f"Another worker holds an active lease on intent '{intent_id}' until {intent.lease_expires_at.isoformat()}."
                        )

            intent.status = UploadIntentStatus.CONFIRMING
            intent.lease_token = lease_token
            intent.lease_expires_at = now + timedelta(seconds=lease_duration_seconds)
            return copy.deepcopy(intent)

    def commit_verified_attachment_atomic(
        self,
        complaint_id: str,
        intent_id: str,
        lease_token: str,
        final_key: str,
        staging_sha256: str,
        staging_size: int,
        now: datetime,
    ) -> Complaint:
        """Atomically update Complaint with attachment and mark Intent as VERIFIED."""
        with self._lock:
            key = (complaint_id, intent_id)
            intent = self._intents.get(key)
            if intent is None:
                raise UploadIntentNotFoundError(
                    f"Upload intent '{intent_id}' for complaint '{complaint_id}' was not found."
                )

            if intent.lease_token != lease_token:
                raise StaleLeaseError(
                    f"Worker lease token mismatch: expected '{intent.lease_token}', got '{lease_token}'. Worker has been fenced out."
                )

            if intent.status == UploadIntentStatus.VERIFIED:
                complaint = self._storage.get(complaint_id)
                if complaint is None:
                    raise ComplaintNotFoundError(f"Complaint '{complaint_id}' was not found.")
                return copy.deepcopy(complaint)

            if intent.status != UploadIntentStatus.CONFIRMING:
                raise UploadFailedError(
                    f"Cannot commit upload intent '{intent_id}' in status '{intent.status.value}'."
                )

            complaint = self._storage.get(complaint_id)
            if complaint is None:
                raise ComplaintNotFoundError(f"Complaint '{complaint_id}' was not found.")

            # Atomically update both complaint and intent
            complaint.attachment_key = final_key
            complaint.attachment_status = "VERIFIED"
            complaint.version += 1

            intent.status = UploadIntentStatus.VERIFIED
            intent.completed_at = now
            intent.staging_sha256 = staging_sha256
            intent.staging_size = staging_size

            return copy.deepcopy(complaint)

    def mark_intent_failed(
        self,
        complaint_id: str,
        intent_id: str,
        reason: str,
        now: datetime,
        lease_token: str | None = None,
    ) -> None:
        """Mark an upload intent as permanently FAILED."""
        with self._lock:
            key = (complaint_id, intent_id)
            intent = self._intents.get(key)
            if intent is None:
                return
            intent.status = UploadIntentStatus.FAILED
            intent.failure_reason = reason
            intent.completed_at = now

    def record_missing_staging_attempt(
        self,
        complaint_id: str,
        intent_id: str,
        max_attempts: int,
        now: datetime,
    ) -> int:
        """Increment confirm_attempts when staging object is missing."""
        with self._lock:
            key = (complaint_id, intent_id)
            intent = self._intents.get(key)
            if intent is None:
                raise UploadIntentNotFoundError(f"Upload intent '{intent_id}' was not found.")

            intent.confirm_attempts += 1
            # Release lease and reset status if still pending so retry can proceed
            intent.lease_token = None
            intent.lease_expires_at = None
            if intent.confirm_attempts >= max_attempts:
                intent.status = UploadIntentStatus.FAILED
                intent.failure_reason = "Exceeded maximum confirmation retry attempts without staging object present"
                intent.completed_at = now
            else:
                intent.status = UploadIntentStatus.PENDING_UPLOAD
            return intent.confirm_attempts

    def clear(self) -> None:
        """Dev/testing utility to wipe the in-memory store."""
        with self._lock:
            self._storage.clear()
            self._intents.clear()
            self._attempts.clear()
            self._outbox.clear()

    # -------------------------------------------------------------------------
    # Stage B2: strongly-consistent reads and shared validation helpers
    # -------------------------------------------------------------------------

    def get_consistent(self, complaint_id: str) -> Complaint | None:
        """Strongly consistent read (in-memory: identical to get())."""
        return self.get(complaint_id)

    def get_intent_consistent(self, complaint_id: str, intent_id: str) -> UploadIntent | None:
        """Strongly consistent read of an UploadIntent (in-memory: identical to get_intent())."""
        return self.get_intent(complaint_id, intent_id)

    @staticmethod
    def _require_confirming_lease(
        intent: UploadIntent,
        lease_token: str,
        now: datetime,
        caller: str,
    ) -> None:
        """Enforce CONFIRMING status + valid unexpired lease (now >= lease_expires_at is stale)."""
        if intent.status == UploadIntentStatus.FAILED:
            raise UploadFailedError(
                f"{caller}: upload intent '{intent.intent_id}' is in terminal FAILED state: "
                f"{intent.failure_reason or 'unknown reason'}."
            )
        if intent.status != UploadIntentStatus.CONFIRMING:
            raise StaleLeaseError(
                f"{caller}: upload intent '{intent.intent_id}' status is '{intent.status.value}', expected CONFIRMING."
            )
        if (
            not lease_token
            or not isinstance(lease_token, str)
            or intent.lease_token != lease_token
            or intent.lease_expires_at is None
            or now >= intent.lease_expires_at
        ):
            raise StaleLeaseError(
                f"{caller}: lease fencing failed for intent '{intent.intent_id}'."
            )

    @staticmethod
    def _collect_intent_immutable_mismatches(
        intent: UploadIntent,
        *,
        source_version_id: str,
        final_key: str,
        normalised_sha256: str,
        expected_mime: str,
        max_byte_size: int,
    ) -> list[str]:
        """Compare caller parameters against UploadIntent's pinned immutable attributes."""
        mismatches: list[str] = []
        if intent.source_version_id != source_version_id:
            mismatches.append(
                f"source_version_id: intent={intent.source_version_id!r} vs {source_version_id!r}"
            )
        if intent.final_key != final_key:
            mismatches.append(f"final_key: intent={intent.final_key!r} vs {final_key!r}")
        intent_sha = normalize_sha256_hex(intent.expected_sha256, "expected_sha256")
        if intent_sha != normalised_sha256:
            mismatches.append(
                f"expected_sha256: intent={intent_sha!r} vs {normalised_sha256!r}"
            )
        if intent.expected_mime != expected_mime:
            mismatches.append(
                f"expected_mime: intent={intent.expected_mime!r} vs {expected_mime!r}"
            )
        if intent.max_byte_size != max_byte_size:
            mismatches.append(
                f"max_byte_size: intent={intent.max_byte_size!r} vs {max_byte_size!r}"
            )
        return mismatches

    @staticmethod
    def _collect_attempt_immutable_mismatches(
        attempt: UploadAttempt,
        *,
        lease_token: str,
        source_version_id: str,
        final_key: str,
        normalised_sha256: str,
        expected_mime: str,
        max_byte_size: int,
    ) -> list[str]:
        """Compare caller parameters against UploadAttempt's six immutable attributes (Spec §6.1)."""
        mismatches: list[str] = []
        if attempt.lease_token != lease_token:
            mismatches.append(f"lease_token: attempt={attempt.lease_token!r} vs {lease_token!r}")
        if attempt.source_version_id != source_version_id:
            mismatches.append(
                f"source_version_id: attempt={attempt.source_version_id!r} vs {source_version_id!r}"
            )
        if attempt.final_key != final_key:
            mismatches.append(f"final_key: attempt={attempt.final_key!r} vs {final_key!r}")
        if attempt.sha256 != normalised_sha256:
            mismatches.append(f"sha256: attempt={attempt.sha256!r} vs {normalised_sha256!r}")
        if attempt.content_type != expected_mime:
            mismatches.append(
                f"content_type: attempt={attempt.content_type!r} vs {expected_mime!r}"
            )
        if attempt.max_byte_size != max_byte_size:
            mismatches.append(
                f"max_byte_size: attempt={attempt.max_byte_size!r} vs {max_byte_size!r}"
            )
        return mismatches

    @staticmethod
    def _validate_version_id(version_id: Any, field_name: str) -> str:
        if not isinstance(version_id, str) or version_id.strip() in ("", "null"):
            raise ValidationError(
                f"'{field_name}' must be a non-empty valid S3 VersionId, got {version_id!r}."
            )
        return version_id.strip()

    # -------------------------------------------------------------------------
    # Stage B2: pin_source_version
    # -------------------------------------------------------------------------

    def pin_source_version(
        self,
        complaint_id: str,
        intent_id: str,
        lease_token: str,
        source_version_id: str,
        now: datetime,
    ) -> UploadIntent:
        """Lease-fenced, idempotent pin of source_version_id on a CONFIRMING intent."""
        parsed_now = parse_utc_iso_datetime(now, "now")
        cleaned_vid = self._validate_version_id(source_version_id, "source_version_id")

        with self._lock:
            intent = self._intents.get((complaint_id, intent_id))
            if intent is None:
                raise UploadIntentNotFoundError(
                    f"Upload intent '{intent_id}' for complaint '{complaint_id}' was not found."
                )

            self._require_confirming_lease(intent, lease_token, parsed_now, "pin_source_version")

            if intent.source_version_id == cleaned_vid:
                return copy.deepcopy(intent)

            if intent.source_version_id is not None:
                raise ImmutableFieldViolationError(
                    f"pin_source_version: intent '{intent_id}' is already pinned to "
                    f"source_version_id='{intent.source_version_id}', "
                    f"cannot overwrite with '{cleaned_vid}'."
                )

            intent.source_version_id = cleaned_vid
            intent.updated_at = parsed_now
            return copy.deepcopy(intent)

    # -------------------------------------------------------------------------
    # Stage B2: begin_copy_attempt
    # -------------------------------------------------------------------------

    def begin_copy_attempt(
        self,
        *,
        complaint_id: str,
        intent_id: str,
        lease_token: str,
        source_version_id: str,
        final_key: str,
        expected_sha256: str,
        expected_mime: str,
        max_byte_size: int,
        now: datetime,
    ) -> UploadAttempt:
        """Lease-fenced, idempotent, CW-2-safe UploadAttempt creation (status=COPYING)."""
        parsed_now = parse_utc_iso_datetime(now, "now")
        cleaned_src_vid = self._validate_version_id(source_version_id, "source_version_id")
        if not isinstance(final_key, str) or not final_key.strip():
            raise ValidationError("begin_copy_attempt: 'final_key' must be a non-empty string.")
        if not isinstance(expected_mime, str) or not expected_mime.strip():
            raise ValidationError("begin_copy_attempt: 'expected_mime' must be a non-empty string.")
        if isinstance(max_byte_size, bool) or not isinstance(max_byte_size, int) or max_byte_size < 1:
            raise ValidationError(
                f"begin_copy_attempt: 'max_byte_size' must be a positive integer, got {max_byte_size!r}."
            )
        normalised_sha = normalize_sha256_hex(expected_sha256, "expected_sha256")

        with self._lock:
            intent = self._intents.get((complaint_id, intent_id))
            if intent is None:
                raise UploadIntentNotFoundError(
                    f"Upload intent '{intent_id}' for complaint '{complaint_id}' was not found."
                )

            self._require_confirming_lease(intent, lease_token, parsed_now, "begin_copy_attempt")

            intent_mismatches = self._collect_intent_immutable_mismatches(
                intent,
                source_version_id=cleaned_src_vid,
                final_key=final_key,
                normalised_sha256=normalised_sha,
                expected_mime=expected_mime,
                max_byte_size=max_byte_size,
            )
            if intent_mismatches:
                raise ImmutableFieldViolationError(
                    f"begin_copy_attempt: UploadIntent mismatch for '{intent_id}': "
                    f"{'; '.join(intent_mismatches)}"
                )

            attempt_key = (complaint_id, intent_id, lease_token)
            existing = self._attempts.get(attempt_key)
            if existing is not None:
                if existing.status != UploadAttemptStatus.COPYING:
                    raise ImmutableFieldViolationError(
                        f"begin_copy_attempt: existing attempt for ({complaint_id}, {intent_id}, {lease_token}) "
                        f"is in status '{existing.status.value}', expected COPYING."
                    )
                attempt_mismatches = self._collect_attempt_immutable_mismatches(
                    existing,
                    lease_token=lease_token,
                    source_version_id=cleaned_src_vid,
                    final_key=final_key,
                    normalised_sha256=normalised_sha,
                    expected_mime=expected_mime,
                    max_byte_size=max_byte_size,
                )
                if attempt_mismatches:
                    raise ImmutableFieldViolationError(
                        f"begin_copy_attempt: idempotency mismatch for attempt "
                        f"({complaint_id}, {intent_id}, {lease_token}): {'; '.join(attempt_mismatches)}"
                    )
                return copy.deepcopy(existing)

            # CW-2: prior attempts under other lease tokens remain intact as audit records.
            attempt = UploadAttempt(
                complaint_id=complaint_id,
                intent_id=intent_id,
                lease_token=lease_token,
                source_version_id=cleaned_src_vid,
                sha256=normalised_sha,
                content_type=expected_mime,
                max_byte_size=max_byte_size,
                final_key=final_key,
                status=UploadAttemptStatus.COPYING,
                created_at=parsed_now,
                updated_at=parsed_now,
            )
            self._attempts[attempt_key] = copy.deepcopy(attempt)
            return copy.deepcopy(attempt)

    # -------------------------------------------------------------------------
    # Stage B2: get_attempt / list_attempts
    # -------------------------------------------------------------------------

    def get_attempt(
        self,
        complaint_id: str,
        intent_id: str,
        lease_token: str,
    ) -> UploadAttempt | None:
        """Retrieve an UploadAttempt by composite key."""
        with self._lock:
            attempt = self._attempts.get((complaint_id, intent_id, lease_token))
            return copy.deepcopy(attempt) if attempt is not None else None

    def list_attempts(
        self,
        complaint_id: str,
        intent_id: str,
    ) -> list[UploadAttempt]:
        """List all UploadAttempts for an intent in deterministic creation order."""
        with self._lock:
            results = [
                copy.deepcopy(a)
                for (cid, iid, _), a in self._attempts.items()
                if cid == complaint_id and iid == intent_id
            ]
            results.sort(key=lambda a: (a.created_at, a.lease_token))
            return results

    # -------------------------------------------------------------------------
    # Stage B2: commit_verified_attachment_versioned
    # -------------------------------------------------------------------------

    def commit_verified_attachment_versioned(
        self,
        *,
        complaint_id: str,
        intent_id: str,
        lease_token: str,
        final_key: str,
        source_version_id: str,
        destination_version_id: str,
        expected_sha256: str,
        expected_mime: str,
        max_byte_size: int,
        expected_complaint_version: int,
        staging_sha256: str,
        staging_size: int,
        now: datetime,
    ) -> AtomicAttachmentCommitResult:
        """Four-action atomic commit. Validates ALL conditions before mutating any state."""
        parsed_now = parse_utc_iso_datetime(now, "now")
        cleaned_src_vid = self._validate_version_id(source_version_id, "source_version_id")
        cleaned_dst_vid = self._validate_version_id(destination_version_id, "destination_version_id")
        if not isinstance(lease_token, str) or not lease_token.strip():
            raise ValidationError("commit_versioned: 'lease_token' must be a non-empty string.")
        if not isinstance(final_key, str) or not final_key.strip():
            raise ValidationError("commit_versioned: 'final_key' must be a non-empty string.")
        if not isinstance(expected_mime, str) or not expected_mime.strip():
            raise ValidationError("commit_versioned: 'expected_mime' must be a non-empty string.")
        if isinstance(expected_complaint_version, bool) or not isinstance(expected_complaint_version, int) or expected_complaint_version < 1:
            raise ValidationError(
                f"commit_versioned: 'expected_complaint_version' must be int >= 1, got {expected_complaint_version!r}."
            )
        if isinstance(max_byte_size, bool) or not isinstance(max_byte_size, int) or max_byte_size < 1:
            raise ValidationError(
                f"commit_versioned: 'max_byte_size' must be int >= 1, got {max_byte_size!r}."
            )
        if isinstance(staging_size, bool) or not isinstance(staging_size, int) or staging_size < 1 or staging_size > max_byte_size:
            raise ValidationError(
                f"commit_versioned: 'staging_size' must be in [1, {max_byte_size}], got {staging_size!r}."
            )
        normalised_expected_sha = normalize_sha256_hex(expected_sha256, "expected_sha256")
        normalised_staging_sha = normalize_sha256_hex(staging_sha256, "staging_sha256")
        if normalised_staging_sha != normalised_expected_sha:
            raise ImmutableFieldViolationError(
                f"commit_versioned: staging_sha256 '{normalised_staging_sha}' != expected_sha256 '{normalised_expected_sha}'."
            )

        with self._lock:
            # -----------------------------------------------------------
            # PRE-VALIDATION: all four action conditions checked first
            # -----------------------------------------------------------
            complaint = self._storage.get(complaint_id)
            if complaint is None:
                raise ComplaintNotFoundError(f"Complaint '{complaint_id}' was not found.")

            intent = self._intents.get((complaint_id, intent_id))
            if intent is None:
                raise UploadIntentNotFoundError(
                    f"Upload intent '{intent_id}' for complaint '{complaint_id}' was not found."
                )

            attempt = self._attempts.get((complaint_id, intent_id, lease_token))
            existing_outbox = self._outbox.get(intent_id)

            # Idempotent replay if THIS exact commit already succeeded (Spec §5.2):
            # Complaint is VERIFIED (matching version = expected or expected+1) and Intent is VERIFIED
            # with matching immutable fields and Attempt is COMMITTED.
            # Note: under CW-5, an already-swept and deleted outbox record is valid *only* when all
            # three committed entities above prove this exact commit already succeeded.
            if (
                complaint.attachment_status == "VERIFIED"
                and intent.status == UploadIntentStatus.VERIFIED
                and complaint.version in (expected_complaint_version, expected_complaint_version + 1)
                and complaint.attachment_key == final_key
                and intent.lease_token == lease_token
                and not self._collect_intent_immutable_mismatches(
                    intent,
                    source_version_id=cleaned_src_vid,
                    final_key=final_key,
                    normalised_sha256=normalised_expected_sha,
                    expected_mime=expected_mime,
                    max_byte_size=max_byte_size,
                )
                and attempt is not None
                and attempt.status == UploadAttemptStatus.COMMITTED
                and attempt.destination_version_id == cleaned_dst_vid
                and not self._collect_attempt_immutable_mismatches(
                    attempt,
                    lease_token=lease_token,
                    source_version_id=cleaned_src_vid,
                    final_key=final_key,
                    normalised_sha256=normalised_expected_sha,
                    expected_mime=expected_mime,
                    max_byte_size=max_byte_size,
                )
                and (
                    existing_outbox is None
                    or (
                        existing_outbox.complaint_id == complaint_id
                        and existing_outbox.intent_id == intent_id
                        and existing_outbox.s3_key == final_key
                        and existing_outbox.destination_version_id == cleaned_dst_vid
                    )
                )
            ):
                return AtomicAttachmentCommitResult(
                    complaint=copy.deepcopy(complaint),
                    intent=copy.deepcopy(intent),
                    attempt=copy.deepcopy(attempt),
                    outbox=copy.deepcopy(existing_outbox) if existing_outbox is not None else None,
                )

            # Lease fencing before Action 1: a fenced-out worker (stolen/mismatched lease_token)
            # or a failed intent must fail with StaleLeaseError / UploadFailedError even if the
            # winning worker already committed Complaint.attachment_status='VERIFIED'.
            if intent.status == UploadIntentStatus.FAILED:
                raise UploadFailedError(
                    f"commit_versioned: upload intent '{intent_id}' has failed: "
                    f"{intent.failure_reason or 'unknown reason'}."
                )
            if intent.lease_token != lease_token:
                raise StaleLeaseError(
                    f"commit_versioned: lease token mismatch for intent '{intent_id}'. Worker fenced out."
                )

            # Action 1: Complaint version & attachment_status guard
            if complaint.version != expected_complaint_version:
                raise OptimisticLockError(
                    f"commit_versioned: complaint '{complaint_id}' version conflict: "
                    f"expected {expected_complaint_version}, stored {complaint.version}."
                )
            if complaint.attachment_status == "VERIFIED":
                raise ImmutableFieldViolationError(
                    f"commit_versioned: complaint '{complaint_id}' attachment_status is already 'VERIFIED'."
                )

            # Action 2: UploadIntent status + immutable field guards
            if intent.status != UploadIntentStatus.CONFIRMING:
                raise StaleLeaseError(
                    f"commit_versioned: intent '{intent_id}' status is '{intent.status.value}', expected CONFIRMING."
                )
            intent_mismatches = self._collect_intent_immutable_mismatches(
                intent,
                source_version_id=cleaned_src_vid,
                final_key=final_key,
                normalised_sha256=normalised_expected_sha,
                expected_mime=expected_mime,
                max_byte_size=max_byte_size,
            )
            if intent_mismatches:
                raise ImmutableFieldViolationError(
                    f"commit_versioned: UploadIntent immutable field mismatch: {'; '.join(intent_mismatches)}"
                )

            # Action 3: UploadAttempt existence + COPYING status + 6 immutable fields
            if attempt is None:
                raise UploadIntentNotFoundError(
                    f"commit_versioned: UploadAttempt not found for "
                    f"({complaint_id}, {intent_id}, {lease_token})."
                )
            if attempt.status != UploadAttemptStatus.COPYING:
                raise ImmutableFieldViolationError(
                    f"commit_versioned: attempt status='{attempt.status.value}', expected COPYING."
                )
            attempt_mismatches = self._collect_attempt_immutable_mismatches(
                attempt,
                lease_token=lease_token,
                source_version_id=cleaned_src_vid,
                final_key=final_key,
                normalised_sha256=normalised_expected_sha,
                expected_mime=expected_mime,
                max_byte_size=max_byte_size,
            )
            if attempt_mismatches:
                raise ImmutableFieldViolationError(
                    f"commit_versioned: UploadAttempt immutable field mismatch: {'; '.join(attempt_mismatches)}"
                )

            # Action 4: Duplicate outbox guard (attribute_not_exists(PK))
            if existing_outbox is not None:
                raise ImmutableFieldViolationError(
                    f"commit_versioned: outbox record already exists for intent '{intent_id}'. Duplicate commit."
                )

            # -----------------------------------------------------------
            # ALL VALIDATIONS PASSED → mutate state atomically
            # -----------------------------------------------------------

            # Action 1: update Complaint
            complaint.attachment_key = final_key
            complaint.attachment_status = "VERIFIED"
            complaint.version += 1

            # Action 2: update UploadIntent
            intent.status = UploadIntentStatus.VERIFIED
            intent.completed_at = parsed_now
            intent.staging_sha256 = normalised_staging_sha
            intent.staging_size = staging_size
            intent.updated_at = parsed_now

            # Action 3: update UploadAttempt
            attempt.status = UploadAttemptStatus.COMMITTED
            attempt.destination_version_id = cleaned_dst_vid
            attempt.updated_at = parsed_now

            # Action 4: create outbox record
            outbox = TagOutboxRecord(
                intent_id=intent_id,
                complaint_id=complaint_id,
                s3_key=final_key,
                destination_version_id=cleaned_dst_vid,
                committed_at=parsed_now,
            )
            self._outbox[intent_id] = outbox

            return AtomicAttachmentCommitResult(
                complaint=copy.deepcopy(complaint),
                intent=copy.deepcopy(intent),
                attempt=copy.deepcopy(attempt),
                outbox=copy.deepcopy(outbox),
            )

    # -------------------------------------------------------------------------
    # Stage B2: Outbox CRUD
    # -------------------------------------------------------------------------

    def get_outbox_record(self, intent_id: str) -> TagOutboxRecord | None:
        """Retrieve the outbox record for an intent, or None."""
        with self._lock:
            rec = self._outbox.get(intent_id)
            return copy.deepcopy(rec) if rec is not None else None

    def list_pending_outbox_records(self, *, limit: int = 100) -> list[TagOutboxRecord]:
        """List up to ``limit`` outbox records sorted deterministically by SK (``intent_id``) ascending."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValidationError(f"list_pending_outbox_records: 'limit' must be int >= 1, got {limit!r}.")
        with self._lock:
            records = [copy.deepcopy(r) for r in self._outbox.values()]
        records.sort(key=lambda r: r.intent_id)
        return records[:limit]

    def list_pending_outbox_records_paginated(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> tuple[list[TagOutboxRecord], str | None]:
        """Paginate pending outbox records ordered by SK (``intent_id``) ascending."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValidationError(
                f"list_pending_outbox_records_paginated: 'limit' must be int >= 1, got {limit!r}."
            )
        after_intent_id: str | None = None
        if cursor is not None:
            if not isinstance(cursor, str) or not cursor:
                raise ValidationError(f"Invalid outbox pagination cursor: {cursor!r}.")
            try:
                padded = cursor + "=" * ((4 - len(cursor) % 4) % 4)
                decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
                if not decoded.startswith("INTENT#") or len(decoded) <= len("INTENT#"):
                    raise ValueError("missing INTENT# prefix")
                after_intent_id = decoded[len("INTENT#") :]
            except Exception as err:
                raise ValidationError(f"Invalid outbox pagination cursor: {cursor!r}.") from err

        with self._lock:
            records = [
                copy.deepcopy(r)
                for r in self._outbox.values()
                if after_intent_id is None or r.intent_id > after_intent_id
            ]
        records.sort(key=lambda r: r.intent_id)
        page = records[:limit]
        next_cursor: str | None = None
        if len(records) > len(page) and page:
            raw_sk = f"INTENT#{page[-1].intent_id}".encode("utf-8")
            next_cursor = base64.urlsafe_b64encode(raw_sk).decode("ascii").rstrip("=")
        return page, next_cursor

    def increment_outbox_sweep_attempt(self, intent_id: str, now: datetime) -> TagOutboxRecord:
        """Atomically increment sweep_attempts and return the updated record."""
        parse_utc_iso_datetime(now, "now")
        with self._lock:
            rec = self._outbox.get(intent_id)
            if rec is None:
                raise UploadIntentNotFoundError(
                    f"increment_outbox_sweep_attempt: no outbox record for intent '{intent_id}'."
                )
            rec.sweep_attempts += 1
            return copy.deepcopy(rec)

    def delete_outbox_record(self, intent_id: str) -> None:
        """Delete the outbox record for intent_id. No-op if not present."""
        with self._lock:
            self._outbox.pop(intent_id, None)
