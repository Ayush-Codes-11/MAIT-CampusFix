"""Repository interfaces for MAIT CampusFix.

Defines the storage contract for complaints. This abstraction allows
swapping between in-memory storage (local dev/testing) and future production
engines like AWS DynamoDB or relational databases without changing any domain logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from campusfix.domain.models import (
    AtomicAttachmentCommitResult,
    Complaint,
    ComplaintStatus,
    IssueCategory,
    TagOutboxRecord,
    UploadAttempt,
    UploadIntent,
)


class ComplaintRepository(ABC):
    """Abstract storage interface for Complaint aggregates.
    
    Architecture Note on Distributed Persistence (AWS DynamoDB):
    ------------------------------------------------------------
    When implementing this interface with Amazon DynamoDB:
    
    1. Table Schema:
       - Single-Table Design:
         * Partition Key (PK): `COMPLAINT#{id}`
         * Sort Key (SK): `METADATA`
       - Global Secondary Indexes:
         * GSI1: PK=`STATUS#{status}`, SK=`SUBMITTED_AT#{iso_datetime}` (status queries)
         * GSI2: PK=`ORIGINAL#{original_id}`, SK=`SUBMITTED_AT#{iso_datetime}` (follow-ups)
    
    2. Overwrite Protection:
       - `create()` uses `PutItem` with `ConditionExpression: "attribute_not_exists(PK)"`.
         If the ID already exists, DynamoDB throws `ConditionalCheckFailedException`,
         which maps to `ComplaintAlreadyExistsError`.
    
    3. Optimistic Concurrency Control (OCC):
       - `update(complaint, expected_version)` executes `PutItem` or `UpdateItem` with:
         `ConditionExpression: "attribute_exists(PK) AND version = :expected_version"`
         and increments `version = version + 1`.
         If another process updated the record concurrently, the condition check fails,
         which maps to `OptimisticLockError`.
    
    4. Concurrency Protection for Upvotes:
       - Upvotes are stored as a DynamoDB String Set (`SS`).
       - To add an upvote:
         `UpdateItem` with `UpdateExpression: "ADD upvotes :student_set"`
       - To remove an upvote:
         `UpdateItem` with `UpdateExpression: "DELETE upvotes :student_set"`
       - DynamoDB Set operations are atomic in-place updates on the database node,
         eliminating read-modify-write lost-update races without distributed locks.
    
    5. Follow-Up Lifecycle & Transactional Lock Item:
       - To guarantee that only ONE active follow-up exists across distributed instances:
         Use `TransactWriteItems`:
         Item 1: Put the new follow-up complaint:
                 `PK: "COMPLAINT#{follow_up_id}", SK: "METADATA"`
                 `ConditionExpression: "attribute_not_exists(PK)"`
         Item 2: Conditional Put of an active follow-up lock item:
                 `PK: "COMPLAINT#{original_id}#ACTIVE_FOLLOW_UP", SK: "LOCK"`
                 `ConditionExpression: "attribute_not_exists(PK)"`
                 Attributes: `{"active_follow_up_id": follow_up_id}`
         Item 3: Parent status verification:
                 `PK: "COMPLAINT#{original_id}", SK: "METADATA"`
                 `ConditionExpression: "attribute_exists(PK) AND #status = :resolved"`
       - Lock Release Lifecycle:
         When the active follow-up transitions to `RESOLVED`, the lock item is deleted
         transactionally using `TransactWriteItems`:
         Action 1: Update follow-up status to `RESOLVED`.
         Action 2: Delete lock item with condition:
                   `DeleteItem PK: "COMPLAINT#{original_id}#ACTIVE_FOLLOW_UP", SK: "LOCK"`
                   `ConditionExpression: "active_follow_up_id = :this_follow_up_id"`
         This prevents deleting a lock belonging to a different or subsequent follow-up!
    """

    @abstractmethod
    def get(self, complaint_id: str) -> Complaint | None:
        """Retrieve a complaint by its unique identifier.
        
        Returns:
            The Complaint object if found, or None if not found.
        """

    @abstractmethod
    def get_consistent(self, complaint_id: str) -> Complaint | None:
        """Strongly consistent read of a Complaint.

        In-memory: identical to ``get()``.
        DynamoDB: ``GetItem`` with ``ConsistentRead=True``.
        """

    @abstractmethod
    def create(self, complaint: Complaint) -> None:
        """Persist a newly created complaint.
        
        Raises:
            ComplaintAlreadyExistsError: If a complaint with this ID already exists.
        """

    @abstractmethod
    def update(self, complaint: Complaint, expected_version: int) -> None:
        """Update an existing complaint under optimistic concurrency control.
        
        Args:
            complaint: The updated Complaint aggregate.
            expected_version: The mandatory positive integer version observed when the caller read the complaint.
                             Must match the currently stored version.
        
        Raises:
            ValidationError: If expected_version is None, boolean, non-integer, or < 1.
            ComplaintNotFoundError: If the complaint does not exist.
            OptimisticLockError: If expected_version does not match stored version.
        """

    @abstractmethod
    def seed(self, complaint: Complaint) -> None:
        """Persist a complaint aggregate for test fixture seeding or initial data loading.
        
        This method is strictly for initial test fixture setup or bootstrapping,
        NOT for normal application updates.
        """

    @abstractmethod
    def save(self, complaint: Complaint) -> None:
        """Deprecated unrestricted save. Overwriting existing complaints is forbidden."""

    @abstractmethod
    def create_follow_up_atomic(
        self,
        original_complaint_id: str,
        follow_up: Complaint,
    ) -> Complaint:
        """Atomically verify parent status and active follow-up invariant and persist follow-up.
        
        Must be executed atomically under a lock (in-memory) or via a transactional
        conditional write (DynamoDB TransactWriteItems).
        
        Enforces:
        - Parent complaint must be in RESOLVED status. Overdue but unresolved complaints
          cannot create follow-ups under any circumstances.
        - Exactly one active (unresolved) follow-up may exist per parent complaint.
        
        Raises:
            ComplaintNotFoundError: If the original complaint does not exist.
            FollowUpEligibilityError: If the parent complaint is not in RESOLVED status.
            ActiveFollowUpExistsError: If an active (unresolved) follow-up exists.
            ComplaintAlreadyExistsError: If follow_up.id already exists.
        """

    @abstractmethod
    def toggle_upvote_atomic(
        self,
        complaint_id: str,
        student_id: str,
    ) -> tuple[Complaint, bool]:
        """Atomically toggle a student's upvote on a complaint.
        
        Eliminates read-modify-save lost updates under concurrent voting.
        
        Returns:
            Tuple of (updated_complaint, upvote_added: bool)
        Raises:
            ComplaintNotFoundError: If complaint_id is not found.
        """

    @abstractmethod
    def add_upvote_atomic(
        self,
        complaint_id: str,
        student_id: str,
    ) -> tuple[Complaint, bool]:
        """Atomically register a student's upvote (Option B - Idempotent).
        
        If the student has already upvoted, returns the existing complaint without
        incrementing version and with changed=False.
        
        Returns:
            Tuple of (updated_complaint, changed: bool)
        Raises:
            ComplaintNotFoundError: If complaint_id is not found.
        """

    @abstractmethod
    def remove_upvote_atomic(
        self,
        complaint_id: str,
        student_id: str,
    ) -> tuple[Complaint, bool]:
        """Atomically withdraw a student's upvote (Option B - Idempotent).
        
        If the student has not upvoted, returns the existing complaint without
        incrementing version and with changed=False.
        
        Returns:
            Tuple of (updated_complaint, changed: bool)
        Raises:
            ComplaintNotFoundError: If complaint_id is not found.
        """

    @abstractmethod
    def list_all(
        self,
        status: ComplaintStatus | None = None,
        building: str | None = None,
        category: IssueCategory | None = None,
    ) -> list[Complaint]:
        """List all complaints, with optional filtering."""

    @abstractmethod
    def list_paginated(
        self,
        status: ComplaintStatus | None = None,
        building: str | None = None,
        category: IssueCategory | None = None,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[Complaint], str | None]:
        """List complaints with indexed filtering (status, building, category) and cursor pagination.
        
        Implementation Note:
        - In-memory pagination uses an unsigned, unencrypted base64 array offset for local testing.
        - Cryptographic HMAC-SHA256 signed DynamoDB ExclusiveStartKey cursor tokens with KMS
          verification are deferred to Step 2.
        
        Returns:
            Tuple of (items, next_cursor or None).
        """

    @abstractmethod
    def find_by_original_id(self, original_complaint_id: str) -> list[Complaint]:
        """Find all follow-up complaints linked to an original complaint ID."""

    @abstractmethod
    def has_active_follow_up(self, original_complaint_id: str) -> bool:
        """Check if an active (unresolved) follow-up exists for an original complaint."""

    @abstractmethod
    def count(self) -> int:
        """Return the total number of complaints stored."""


class UploadIntentRepository(ABC):
    """Abstract storage interface for UploadIntent entities and atomic attachment commits."""

    @abstractmethod
    def create_intent(self, intent: UploadIntent) -> None:
        """Persist a new upload intent in PENDING_UPLOAD status.
        
        Raises:
            ComplaintNotFoundError: If the associated complaint does not exist.
        """

    @abstractmethod
    def get_intent(self, complaint_id: str, intent_id: str) -> UploadIntent | None:
        """Retrieve an upload intent by complaint ID and intent ID."""

    @abstractmethod
    def claim_intent_lease(
        self,
        complaint_id: str,
        intent_id: str,
        lease_token: str,
        lease_duration_seconds: int,
        now: datetime,
    ) -> UploadIntent:
        """Atomically claim or takeover the lease on an upload intent for confirmation.
        
        Evaluates condition:
        - status == PENDING_UPLOAD and now <= confirmation_deadline, OR
        - status == CONFIRMING and lease_expires_at < now and now <= recovery_deadline (takeover).
        
        Sets status = CONFIRMING, lease_token = lease_token, lease_expires_at = now + lease_duration.
        
        Raises:
            UploadIntentNotFoundError: If intent does not exist.
            UploadIntentExpiredError: If confirmation deadline or recovery deadline has passed.
            StaleLeaseError: If another active worker currently holds the unexpired lease.
            UploadFailedError: If the intent is in terminal FAILED status.
        """

    @abstractmethod
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
        """Atomically update the Complaint with verified attachment metadata and mark Intent as VERIFIED.
        
        Guarded by fencing token:
        - Intent must have status == CONFIRMING and lease_token == lease_token.
        
        Atomically:
        1. Sets Complaint.attachment_key = final_key, attachment_status = 'VERIFIED', version += 1.
        2. Sets Intent.status = VERIFIED, completed_at = now, final_sha256 = staging_sha256, staging_size = staging_size.
        
        Raises:
            UploadIntentNotFoundError: If intent does not exist.
            StaleLeaseError: If the worker's lease_token no longer matches (fenced out).
            ComplaintNotFoundError: If parent complaint is missing.
        """

    @abstractmethod
    def mark_intent_failed(
        self,
        complaint_id: str,
        intent_id: str,
        reason: str,
        now: datetime,
        lease_token: str | None = None,
    ) -> None:
        """Mark an upload intent as permanently FAILED."""

    @abstractmethod
    def record_missing_staging_attempt(
        self,
        complaint_id: str,
        intent_id: str,
        max_attempts: int,
        now: datetime,
    ) -> int:
        """Atomically increment confirm_attempts when staging object is not yet found in S3.
        
        If confirm_attempts reaches max_attempts, transitions intent to FAILED.
        
        Returns:
            The incremented attempt count.
        """

    # -------------------------------------------------------------------------
    # Stage B2: strongly-consistent reads and versioned upload persistence
    # -------------------------------------------------------------------------

    @abstractmethod
    def get_intent_consistent(self, complaint_id: str, intent_id: str) -> UploadIntent | None:
        """Strongly consistent read of an UploadIntent.

        In-memory: identical to ``get_intent()``.
        DynamoDB: ``GetItem`` with ``ConsistentRead=True``.
        """

    @abstractmethod
    def pin_source_version(
        self,
        complaint_id: str,
        intent_id: str,
        lease_token: str,
        source_version_id: str,
        now: datetime,
    ) -> UploadIntent:
        """Lease-fenced, idempotent pin of ``source_version_id`` on a CONFIRMING intent.

        Invariants:
        - Lease-fenced: ``now >= intent.lease_expires_at``, missing lease, non-CONFIRMING
          status, or mismatched ``lease_token`` → ``StaleLeaseError`` (or ``UploadFailedError``
          if intent is FAILED).
        - Idempotent: already pinned to the same ``source_version_id`` → return current intent.
        - Immutable: already pinned to a *different* ``source_version_id`` → ``ImmutableFieldViolationError``.
        - Rejects empty / None / ``"null"`` ``source_version_id`` → ``ValidationError``.

        Raises:
            UploadIntentNotFoundError: If the intent does not exist.
            StaleLeaseError: If the lease has expired or the token/status does not match.
            UploadFailedError: If the intent is in terminal FAILED status.
            ImmutableFieldViolationError: If already pinned to a conflicting version ID.
            ValidationError: If ``source_version_id`` or ``now`` is invalid.
        """

    @abstractmethod
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
        """Lease-fenced, idempotent, CW-2-safe creation of an ``UploadAttempt`` with status COPYING.

        Idempotency key: ``(complaint_id, intent_id, lease_token)``.

        Invariants:
        - Lease-fenced (same as ``pin_source_version``).
        - Intent status must be CONFIRMING and all immutable intent fields must match.
        - Idempotent: same key with COPYING status and matching 6 immutable fields → return existing attempt.
        - Immutable: same key with any differing field or non-COPYING status → ``ImmutableFieldViolationError``.
        - CW-2 safety: prior COPYING attempts under a *different* ``lease_token`` are left intact.

        Note: ``expected_complaint_version`` is NOT stored on ``UploadAttempt`` (Conflict 1 resolution).

        Raises:
            UploadIntentNotFoundError: If the intent does not exist.
            StaleLeaseError: If lease-fencing fails.
            UploadFailedError: If the intent is in terminal FAILED status.
            ImmutableFieldViolationError: If intent or existing attempt immutable fields mismatch.
            ValidationError: If inputs fail validation.
        """

    @abstractmethod
    def get_attempt(
        self,
        complaint_id: str,
        intent_id: str,
        lease_token: str,
    ) -> UploadAttempt | None:
        """Retrieve an ``UploadAttempt`` by its composite key."""

    @abstractmethod
    def list_attempts(
        self,
        complaint_id: str,
        intent_id: str,
    ) -> list[UploadAttempt]:
        """List all ``UploadAttempt`` records for an intent, in creation order."""

    @abstractmethod
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
        """Four-action atomic commit using ONLY pre-copy values (Spec §7).

        Pre-validates ALL four action conditions before mutating any state.
        Uses NO post-copy reads to reconstruct transaction inputs.

        Action 1 (Complaint): version guard + set attachment_key / attachment_status=VERIFIED / version+=1.
        Action 2 (UploadIntent): lease+status guard → VERIFIED + completed_at + staging metadata.
        Action 3 (UploadAttempt): status=COPYING + all 6 immutable fields → COMMITTED + destination_version_id.
        Action 4 (Outbox): attribute_not_exists guard → new TagOutboxRecord.

        Raises:
            ComplaintNotFoundError: Complaint missing.
            UploadIntentNotFoundError: Intent or Attempt missing.
            OptimisticLockError: complaint.version != expected_complaint_version.
            StaleLeaseError: lease or intent-status mismatch on Action 2.
            UploadFailedError: Intent is in terminal FAILED status.
            ImmutableFieldViolationError: source_version_id, immutable fields, or duplicate outbox mismatch.
            ValidationError: Invalid inputs (versions, sizes, checksums, timestamps).
        """

    # -------------------------------------------------------------------------
    # Outbox CRUD
    # -------------------------------------------------------------------------

    @abstractmethod
    def get_outbox_record(self, intent_id: str) -> TagOutboxRecord | None:
        """Retrieve the outbox record for an intent, or None if not present."""

    @abstractmethod
    def list_pending_outbox_records(self, *, limit: int = 100) -> list[TagOutboxRecord]:
        """List up to ``limit`` outbox records sorted deterministically by SK (``intent_id``) ascending."""

    @abstractmethod
    def list_pending_outbox_records_paginated(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> tuple[list[TagOutboxRecord], str | None]:
        """Paginate pending outbox records ordered by SK (``intent_id``) ascending.

        Implementations use live key-based pagination (e.g., ``LastEvaluatedKey``
        in DynamoDB) wrapped in an authenticated cursor; no cross-page snapshot
        isolation is guaranteed across concurrent inserts or deletes.
        """

    @abstractmethod
    def increment_outbox_sweep_attempt(self, intent_id: str, now: datetime) -> TagOutboxRecord:
        """Atomically increment ``sweep_attempts`` and return the updated record.

        Raises:
            UploadIntentNotFoundError: If no outbox record exists for ``intent_id``.
        """

    @abstractmethod
    def delete_outbox_record(self, intent_id: str) -> None:
        """Delete the outbox record for ``intent_id``. No-op if not present."""
