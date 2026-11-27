"""Unit tests for repository safety, overwrite protection, and versioning."""

from datetime import datetime, timezone
import unittest

from campusfix.domain.models import (
    Complaint,
    ComplaintFeedback,
    ComplaintStatus,
    IssueCategory,
    ValidationError,
)
from campusfix.domain.rules import (
    ComplaintAlreadyExistsError,
    ComplaintNotFoundError,
    FollowUpEligibilityError,
    InvalidFollowUpReasonError,
    OptimisticLockError,
    UnauthorizedError,
)
from campusfix.repositories.memory import InMemoryComplaintRepository


def make_sample_complaint(cid: str = "CF-2026-TEST01") -> Complaint:
    return Complaint(
        id=cid,
        title="Test Complaint",
        description="This is a test complaint description with more than 20 characters.",
        building="Block A",
        floor="1st Floor",
        room_number="101",
        category=IssueCategory.ELECTRICAL,
        status=ComplaintStatus.PENDING,
        submitted_at=datetime.now(timezone.utc),
        submitted_by="stu-001",
        overdue_threshold_days=7,
        version=1,
    )


class TestRepositorySafety(unittest.TestCase):
    """Tests repository overwrite protection, versioning, and optimistic concurrency."""

    def setUp(self) -> None:
        self.repo = InMemoryComplaintRepository()

    def test_create_persists_new_complaint(self):
        """create() successfully persists a new record."""
        c = make_sample_complaint("CF-2026-0001")
        self.repo.create(c)
        persisted = self.repo.get("CF-2026-0001")
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.id, "CF-2026-0001")

    def test_create_raises_error_on_existing_id(self):
        """create() must raise ComplaintAlreadyExistsError if ID is already in repository."""
        c1 = make_sample_complaint("CF-2026-DUPLICATE")
        self.repo.create(c1)

        c2 = make_sample_complaint("CF-2026-DUPLICATE")
        c2.title = "Attempted overwrite with different title"

        with self.assertRaises(ComplaintAlreadyExistsError):
            self.repo.create(c2)

        # Original item must remain untouched
        persisted = self.repo.get("CF-2026-DUPLICATE")
        self.assertEqual(persisted.title, "Test Complaint")

    def test_update_increments_version_once_on_success(self):
        """update() updates record and increments version counter exactly once."""
        c = make_sample_complaint("CF-2026-VER01")
        self.repo.create(c)
        self.assertEqual(c.version, 1)

        c.title = "Updated title"
        self.repo.update(c, expected_version=1)

        persisted = self.repo.get("CF-2026-VER01")
        self.assertEqual(persisted.version, 2)
        self.assertEqual(persisted.title, "Updated title")

    def test_update_rejects_stale_expected_version(self):
        """update() must reject an update when expected_version does not match stored version."""
        c = make_sample_complaint("CF-2026-STALE01")
        self.repo.create(c)
        self.assertEqual(c.version, 1)

        # Advance version in repo to 2
        c_copy = self.repo.get("CF-2026-STALE01")
        c_copy.title = "First update"
        self.repo.update(c_copy, expected_version=1)

        # Now stored version is 2. A caller presenting expected_version=1 must be rejected
        c.title = "Stale concurrent update"
        with self.assertRaises(OptimisticLockError) as ctx:
            self.repo.update(c, expected_version=1)
        self.assertIn("Version conflict", str(ctx.exception))

        # Stored data must remain what the first update wrote
        persisted = self.repo.get("CF-2026-STALE01")
        self.assertEqual(persisted.version, 2)
        self.assertEqual(persisted.title, "First update")

    def test_two_callers_with_same_version_cannot_both_update(self):
        """Two callers reading version 1: first succeeds, second fails with OptimisticLockError."""
        c = make_sample_complaint("CF-2026-RACE01")
        self.repo.create(c)

        # Both callers load version 1
        caller_a = self.repo.get("CF-2026-RACE01")
        caller_b = self.repo.get("CF-2026-RACE01")
        self.assertEqual(caller_a.version, 1)
        self.assertEqual(caller_b.version, 1)

        # Caller A updates first
        caller_a.title = "Caller A update"
        self.repo.update(caller_a, expected_version=caller_a.version)

        # Caller B attempts update with stale version 1
        caller_b.title = "Caller B update"
        with self.assertRaises(OptimisticLockError):
            self.repo.update(caller_b, expected_version=caller_b.version)

        # Verify Caller A's update was preserved and version is 2
        persisted = self.repo.get("CF-2026-RACE01")
        self.assertEqual(persisted.version, 2)
        self.assertEqual(persisted.title, "Caller A update")

    def test_update_raises_error_on_missing_id(self):
        """update() must raise ComplaintNotFoundError if target ID does not exist."""
        c = make_sample_complaint("CF-2026-NONEXISTENT")
        with self.assertRaises(ComplaintNotFoundError):
            self.repo.update(c, expected_version=1)

    def test_toggle_upvote_increments_version(self):
        """toggle_upvote_atomic() increments version on each vote toggle."""
        c = make_sample_complaint("CF-2026-UPVER")
        self.repo.create(c)

        c_voted, added = self.repo.toggle_upvote_atomic("CF-2026-UPVER", "stu-100")
        self.assertTrue(added)
        self.assertEqual(c_voted.version, 2)

        c_unvoted, added2 = self.repo.toggle_upvote_atomic("CF-2026-UPVER", "stu-100")
        self.assertFalse(added2)
        self.assertEqual(c_unvoted.version, 3)

    def test_create_follow_up_atomic_validates_parent_resolved_status(self):
        """create_follow_up_atomic() must reject follow-up if parent is not Resolved under the lock."""
        parent = make_sample_complaint("CF-2026-PARENT-PENDING")
        parent.status = ComplaintStatus.PENDING
        self.repo.create(parent)

        fu = make_sample_complaint("CF-2026-FU-01")
        fu.is_follow_up = True
        fu.original_complaint_id = parent.id
        fu.follow_up_reason = "Follow up attempt on pending ticket"

        with self.assertRaises(FollowUpEligibilityError):
            self.repo.create_follow_up_atomic(parent.id, fu)


    def test_update_rejects_missing_or_invalid_expected_version(self):
        """update() must reject omitted, None, boolean, float, or invalid expected versions."""
        c = make_sample_complaint("CF-2026-VALVER")
        self.repo.create(c)

        # Omitted expected_version argument
        with self.assertRaises(TypeError):
            self.repo.update(c)  # type: ignore

        # Invalid versions: None, bool, float, <=0, string
        bad_versions = [None, True, False, 0, -1, -99, 1.5, "1", [1]]
        for bad_ver in bad_versions:
            with self.subTest(bad_version=bad_ver):
                with self.assertRaises(ValidationError) as ctx:
                    self.repo.update(c, expected_version=bad_ver)  # type: ignore
                self.assertIn("expected_version", str(ctx.exception))

    def test_save_rejects_unrestricted_overwrite_of_existing_complaint(self):
        """save() must reject unrestricted overwrites of existing records."""
        c = make_sample_complaint("CF-2026-NO-OVERWRITE")
        self.repo.create(c)

        # Calling save() on an existing ID must raise ValidationError
        c_modified = make_sample_complaint("CF-2026-NO-OVERWRITE")
        c_modified.title = "Attempted overwrite via save"
        with self.assertRaises(ValidationError) as ctx:
            self.repo.save(c_modified)
        self.assertIn("Unrestricted overwrite", str(ctx.exception))

        # Original record must remain intact after rejected save()
        persisted = self.repo.get("CF-2026-NO-OVERWRITE")
        self.assertEqual(persisted.title, "Test Complaint")

    def test_create_follow_up_atomic_validates_all_child_invariants(self):
        """create_follow_up_atomic() enforces all child invariants under the repository lock."""
        parent = make_sample_complaint("CF-2026-PARENT-OK")
        parent.status = ComplaintStatus.RESOLVED
        parent.resolved_at = datetime.now(timezone.utc)
        parent.resolution_remarks = "Fixed completely."
        parent.submitted_by = "stu-owner-1"
        self.repo.create(parent)

        # 1. Child ID equals parent ID
        fu_same_id = make_sample_complaint("CF-2026-PARENT-OK")
        fu_same_id.is_follow_up = True
        fu_same_id.original_complaint_id = parent.id
        fu_same_id.follow_up_reason = "Valid reason with enough characters"
        fu_same_id.submitted_by = "stu-owner-1"
        with self.assertRaises(ValidationError) as ctx:
            self.repo.create_follow_up_atomic(parent.id, fu_same_id)
        self.assertIn("cannot be identical to parent ID", str(ctx.exception))

        # 2. Child is_follow_up is False
        fu_not_fu = make_sample_complaint("CF-2026-CHILD-01")
        fu_not_fu.is_follow_up = False
        fu_not_fu.original_complaint_id = parent.id
        fu_not_fu.follow_up_reason = "Valid reason with enough characters"
        fu_not_fu.submitted_by = "stu-owner-1"
        with self.assertRaises(ValidationError) as ctx:
            self.repo.create_follow_up_atomic(parent.id, fu_not_fu)
        self.assertIn("must have is_follow_up=True", str(ctx.exception))

        # 3. Child original_complaint_id mismatch
        fu_mismatch_orig = make_sample_complaint("CF-2026-CHILD-02")
        fu_mismatch_orig.is_follow_up = True
        fu_mismatch_orig.original_complaint_id = "CF-2026-SOME-OTHER-PARENT"
        fu_mismatch_orig.follow_up_reason = "Valid reason with enough characters"
        fu_mismatch_orig.submitted_by = "stu-owner-1"
        with self.assertRaises(ValidationError) as ctx:
            self.repo.create_follow_up_atomic(parent.id, fu_mismatch_orig)
        self.assertIn("must match parent ID", str(ctx.exception))

        # 4. Child status not PENDING
        fu_status_not_pending = make_sample_complaint("CF-2026-CHILD-03")
        fu_status_not_pending.is_follow_up = True
        fu_status_not_pending.original_complaint_id = parent.id
        fu_status_not_pending.status = ComplaintStatus.IN_PROGRESS
        fu_status_not_pending.follow_up_reason = "Valid reason with enough characters"
        fu_status_not_pending.submitted_by = "stu-owner-1"
        with self.assertRaises(ValidationError) as ctx:
            self.repo.create_follow_up_atomic(parent.id, fu_status_not_pending)
        self.assertIn("must start in 'pending' status", str(ctx.exception))

        # 5. Child follow_up_reason invalid
        fu_short_reason = make_sample_complaint("CF-2026-CHILD-04")
        fu_short_reason.is_follow_up = True
        fu_short_reason.original_complaint_id = parent.id
        fu_short_reason.follow_up_reason = "too short"
        fu_short_reason.submitted_by = "stu-owner-1"
        with self.assertRaises(InvalidFollowUpReasonError):
            self.repo.create_follow_up_atomic(parent.id, fu_short_reason)

        # 6. Child submitter does not match parent owner
        fu_wrong_submitter = make_sample_complaint("CF-2026-CHILD-05")
        fu_wrong_submitter.is_follow_up = True
        fu_wrong_submitter.original_complaint_id = parent.id
        fu_wrong_submitter.follow_up_reason = "Valid reason with enough characters"
        fu_wrong_submitter.submitted_by = "stu-imposter-2"
        with self.assertRaises(UnauthorizedError) as ctx:
            self.repo.create_follow_up_atomic(parent.id, fu_wrong_submitter)
        self.assertIn("does not match original complaint owner", str(ctx.exception))

        # 7. Child inherits upvotes
        fu_with_upvotes = make_sample_complaint("CF-2026-CHILD-06")
        fu_with_upvotes.is_follow_up = True
        fu_with_upvotes.original_complaint_id = parent.id
        fu_with_upvotes.follow_up_reason = "Valid reason with enough characters"
        fu_with_upvotes.submitted_by = "stu-owner-1"
        fu_with_upvotes.upvotes = ["stu-voter-1"]
        with self.assertRaises(ValidationError) as ctx:
            self.repo.create_follow_up_atomic(parent.id, fu_with_upvotes)
        self.assertIn("must start with zero upvotes", str(ctx.exception))

        # 8. Child inherits feedback
        fu_with_feedback = make_sample_complaint("CF-2026-CHILD-07")
        fu_with_feedback.is_follow_up = True
        fu_with_feedback.original_complaint_id = parent.id
        fu_with_feedback.follow_up_reason = "Valid reason with enough characters"
        fu_with_feedback.submitted_by = "stu-owner-1"
        fu_with_feedback.feedback = ComplaintFeedback(rating=5, submitted_at=datetime.now(timezone.utc))
        with self.assertRaises(ValidationError) as ctx:
            self.repo.create_follow_up_atomic(parent.id, fu_with_feedback)
        self.assertIn("cannot inherit or contain feedback", str(ctx.exception))


# -----------------------------------------------------------------------------
# Phase 2.2 — seed() duplicate rejection
# -----------------------------------------------------------------------------

class TestSeedSafety(unittest.TestCase):
    """Tests that seed() rejects duplicate IDs and stores unique fixtures correctly."""

    def setUp(self) -> None:
        self.repo = InMemoryComplaintRepository()

    def test_seed_persists_new_complaint(self):
        """seed() successfully stores a complaint that does not yet exist."""
        c = make_sample_complaint("CF-2026-SEED-01")
        self.repo.seed(c)

        persisted = self.repo.get("CF-2026-SEED-01")
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.id, "CF-2026-SEED-01")
        self.assertEqual(persisted.title, "Test Complaint")

    def test_seed_stores_deep_copy(self):
        """seed() stores a defensive copy; post-seed mutations do not alter stored record."""
        c = make_sample_complaint("CF-2026-SEED-02")
        self.repo.seed(c)

        # Mutate original after seeding
        c.title = "Mutated after seed"

        persisted = self.repo.get("CF-2026-SEED-02")
        self.assertEqual(persisted.title, "Test Complaint",
                         "Stored record must not be affected by post-seed caller mutations.")

    def test_seed_rejects_duplicate_id(self):
        """seed() must raise ComplaintAlreadyExistsError when the ID already exists."""
        c1 = make_sample_complaint("CF-2026-SEED-DUP")
        self.repo.seed(c1)

        c2 = make_sample_complaint("CF-2026-SEED-DUP")
        c2.title = "Second fixture attempting overwrite"

        with self.assertRaises(ComplaintAlreadyExistsError) as ctx:
            self.repo.seed(c2)
        self.assertIn("CF-2026-SEED-DUP", str(ctx.exception))

        # Original record must remain intact
        persisted = self.repo.get("CF-2026-SEED-DUP")
        self.assertEqual(persisted.title, "Test Complaint",
                         "Existing record must not be overwritten when seed() is rejected.")

    def test_seed_multiple_distinct_ids_all_persisted(self):
        """seed() persists each unique ID independently without interference."""
        for i in range(1, 6):
            cid = f"CF-2026-SEED-M{i:02d}"
            c = make_sample_complaint(cid)
            self.repo.seed(c)

        self.assertEqual(self.repo.count(), 5)
        for i in range(1, 6):
            cid = f"CF-2026-SEED-M{i:02d}"
            self.assertIsNotNone(self.repo.get(cid))

    def test_seed_after_clear_allows_same_id(self):
        """After clear(), the same ID may be re-seeded without error."""
        c = make_sample_complaint("CF-2026-SEED-REUSE")
        self.repo.seed(c)
        self.repo.clear()

        c2 = make_sample_complaint("CF-2026-SEED-REUSE")
        c2.title = "Re-seeded complaint after clear"
        self.repo.seed(c2)

        persisted = self.repo.get("CF-2026-SEED-REUSE")
        self.assertEqual(persisted.title, "Re-seeded complaint after clear")

    def test_initial_complaints_constructor_rejects_duplicate_seed(self):
        """InMemoryComplaintRepository(initial_complaints=[...]) rejects duplicate IDs at construction."""
        c1 = make_sample_complaint("CF-2026-CTOR-DUP")
        c2 = make_sample_complaint("CF-2026-CTOR-DUP")
        c2.title = "Duplicate for constructor test"

        with self.assertRaises(ComplaintAlreadyExistsError):
            InMemoryComplaintRepository(initial_complaints=[c1, c2])


if __name__ == "__main__":
    unittest.main()
