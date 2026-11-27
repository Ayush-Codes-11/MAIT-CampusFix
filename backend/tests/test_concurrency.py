"""Unit and integration tests for concurrency protection in MAIT CampusFix."""

import concurrent.futures
import threading
import unittest

from campusfix.domain.models import (
    Actor,
    ComplaintStatus,
    IssueCategory,
)
from campusfix.domain.rules import (
    ActiveFollowUpExistsError,
    OptimisticLockError,
    TerminalStatusError,
)
from campusfix.repositories.memory import InMemoryComplaintRepository
from campusfix.services.complaints import ComplaintService


class TestConcurrencyProtection(unittest.TestCase):
    """Tests race condition protection for follow-ups, upvoting, and status updates."""

    def setUp(self) -> None:
        self.repo = InMemoryComplaintRepository()
        self.service = ComplaintService(repository=self.repo)

    def test_concurrent_follow_up_race_condition_eliminated(self):
        """Simulate concurrent follow-up attempts on the same resolved complaint.
        
        Under heavy concurrency, exactly ONE request must succeed in creating
        an active follow-up; all other concurrent requests must receive
        ActiveFollowUpExistsError.
        """
        student = Actor.student("stu-lead")
        staff = Actor.maintenance("staff-fix")

        # Create and resolve parent complaint
        parent = self.service.create_complaint(
            title="HVAC cooling breakdown",
            description="Main auditorium AC compressors have tripped completely.",
            building="Auditorium",
            floor="Ground",
            room_number="Auditorium Hall",
            category=IssueCategory.ELECTRICAL,
            actor=student,
        )
        self.service.transition_status(parent.id, ComplaintStatus.IN_PROGRESS, actor=staff)
        self.service.transition_status(
            parent.id, ComplaintStatus.RESOLVED, actor=staff, remarks="Re-engaged circuit breaker."
        )

        num_threads = 20
        successful_follow_ups: list[str] = []
        conflicts: list[str] = []

        def attempt_follow_up(idx: int) -> str | None:
            try:
                fu = self.service.create_follow_up(
                    original_complaint_id=parent.id,
                    follow_up_reason=f"Concurrent attempt #{idx}: AC stopped again immediately.",
                    actor=student,
                )
                return fu.id
            except ActiveFollowUpExistsError:
                return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(attempt_follow_up, i) for i in range(num_threads)]
            for f in concurrent.futures.as_completed(futures):
                res = f.result()
                if res:
                    successful_follow_ups.append(res)
                else:
                    conflicts.append("Conflict")

        # Exactly ONE follow-up must have been created
        self.assertEqual(len(successful_follow_ups), 1, "Only one concurrent follow-up should succeed!")
        self.assertEqual(len(conflicts), num_threads - 1)

        # Database must show exactly 1 linked follow-up
        linked = self.service.get_linked_follow_ups(parent.id)
        self.assertEqual(len(linked), 1)
        self.assertEqual(linked[0].id, successful_follow_ups[0])

    def test_follow_up_creation_after_earlier_follow_up_resolved(self):
        """Once an earlier active follow-up is resolved, a subsequent follow-up must succeed."""
        student = Actor.student("stu-lead")
        staff = Actor.maintenance("staff-fix")

        parent = self.service.create_complaint(
            title="Lab PC reboot loop",
            description="All PCs in lab 3 are caught in an automatic reboot cycle.",
            building="Block C",
            floor="3rd",
            room_number="Lab 3",
            category=IssueCategory.IT_NETWORK,
            actor=student,
        )
        self.service.transition_status(parent.id, ComplaintStatus.IN_PROGRESS, actor=staff)
        self.service.transition_status(parent.id, ComplaintStatus.RESOLVED, actor=staff, remarks="Fixed image.")

        # Raise first follow-up
        fu1 = self.service.create_follow_up(
            original_complaint_id=parent.id,
            follow_up_reason="PC 12 and 14 are still boot looping after image update.",
            actor=student,
        )
        self.assertEqual(fu1.status, ComplaintStatus.PENDING)

        # Trying to raise second follow-up now must fail
        with self.assertRaises(ActiveFollowUpExistsError):
            self.service.create_follow_up(
                original_complaint_id=parent.id,
                follow_up_reason="Attempting second follow up while first is open",
                actor=student,
            )

        # Resolve first follow-up
        self.service.transition_status(fu1.id, ComplaintStatus.IN_PROGRESS, actor=staff)
        self.service.transition_status(
            fu1.id, ComplaintStatus.RESOLVED, actor=staff, remarks="Replaced bad RAM sticks in PC 12 and 14."
        )

        # Now second follow-up is permitted
        fu2 = self.service.create_follow_up(
            original_complaint_id=parent.id,
            follow_up_reason="PC 12 hard drive failed after RAM replacement.",
            actor=student,
        )
        self.assertEqual(fu2.status, ComplaintStatus.PENDING)
        self.assertEqual(len(self.service.get_linked_follow_ups(parent.id)), 2)

    def test_concurrent_status_transitions_protected_by_optimistic_locking(self):
        """Simulate concurrent staff members attempting to resolve the same complaint.
        
        Exactly one transition must succeed; concurrent attempts with stale expected_version
        must be caught by OptimisticLockError or TerminalStatusError.
        """
        student = Actor.student("stu-test")
        staff_a = Actor.maintenance("staff-a")
        staff_b = Actor.maintenance("staff-b")

        complaint = self.service.create_complaint(
            title="Elevator stuck on 3rd floor",
            description="Passenger elevator in Main Block is immobilized between floors.",
            building="Main Block",
            floor="3rd",
            room_number="Lift B",
            category=IssueCategory.ELECTRICAL,
            actor=student,
        )
        self.service.transition_status(complaint.id, ComplaintStatus.IN_PROGRESS, actor=staff_a)

        successes: list[str] = []
        conflicts: list[str] = []

        def resolve_attempt(staff_actor: Actor, remark: str) -> None:
            try:
                self.service.transition_status(
                    complaint.id,
                    to_status=ComplaintStatus.RESOLVED,
                    actor=staff_actor,
                    remarks=remark,
                )
                successes.append(staff_actor.user_id)
            except (OptimisticLockError, TerminalStatusError) as e:
                conflicts.append(type(e).__name__)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(resolve_attempt, staff_a, "Staff A: Replaced fuse.")
            f2 = executor.submit(resolve_attempt, staff_b, "Staff B: Reset controller.")
            f1.result()
            f2.result()

        # Exactly one resolve operation must succeed
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(conflicts), 1)

        # Final complaint must be in RESOLVED status
        final_complaint = self.service.get_complaint(complaint.id)
        self.assertIsNotNone(final_complaint)
        self.assertEqual(final_complaint.status, ComplaintStatus.RESOLVED)

    def test_concurrent_upvoting_no_lost_updates(self):
        """Simulate 50 concurrent students voting on the same complaint.
        
        Under read-modify-save race conditions, votes are lost.
        Atomic repository execution ensures all 50 votes are safely preserved.
        """
        creator = Actor.student("creator-stu")
        complaint = self.service.create_complaint(
            title="Broken main gate barrier",
            description="Boom barrier at the campus main entrance is stuck midway.",
            building="Main Gate",
            floor="Ground",
            room_number="Gate 1",
            category=IssueCategory.CIVIL_STRUCTURAL,
            actor=creator,
        )

        num_voters = 50
        voter_actors = [Actor.student(f"voter-{i:03d}") for i in range(num_voters)]

        def vote(actor: Actor) -> bool:
            _, added = self.service.toggle_upvote(complaint.id, actor=actor)
            return added

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            results = list(executor.map(vote, voter_actors))

        # All 50 additions succeeded
        self.assertTrue(all(results))

        # Check final complaint state in repository
        fresh = self.service.get_complaint(complaint.id)
        self.assertIsNotNone(fresh)
        self.assertEqual(len(fresh.upvotes), num_voters)
        for voter in voter_actors:
            self.assertIn(voter.user_id, fresh.upvotes)

    def test_concurrent_upvote_toggle_removal(self):
        """20 students who voted concurrently remove their votes."""
        complaint = self.service.create_complaint(
            title="Water dispenser filter dirty",
            description="Water tastes muddy and filter replacement light is flashing.",
            building="Block B",
            floor="1st",
            room_number="Corridor",
            category=IssueCategory.PLUMBING,
            actor=Actor.student("stu-orig"),
        )

        num_students = 20
        students = [Actor.student(f"student-{i:02d}") for i in range(num_students)]

        # Pre-seed 20 votes
        for s in students:
            self.service.toggle_upvote(complaint.id, actor=s)

        fresh_before = self.service.get_complaint(complaint.id)
        self.assertEqual(len(fresh_before.upvotes), num_students)

        # Concurrently remove all 20 votes
        def unvote(actor: Actor) -> bool:
            _, added = self.service.toggle_upvote(complaint.id, actor=actor)
            return added

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(unvote, students))

        # All toggles should indicate removal (added == False)
        self.assertTrue(all(not r for r in results))

        fresh_after = self.service.get_complaint(complaint.id)
        self.assertEqual(len(fresh_after.upvotes), 0)

    def test_concurrent_feedback_updates_protected_by_optimistic_locking(self):
        """Concurrent feedback submissions on the same complaint are protected by optimistic concurrency."""
        student = Actor.student("stu-feedback-tester")
        staff = Actor.maintenance("staff-resolver")

        complaint = self.service.create_complaint(
            title="Broken classroom window",
            description="Window glass is cracked and dangerous for students nearby.",
            building="Block B",
            floor="2nd",
            room_number="203",
            category=IssueCategory.CIVIL_STRUCTURAL,
            actor=student,
        )
        self.service.transition_status(complaint.id, ComplaintStatus.IN_PROGRESS, actor=staff)
        self.service.transition_status(
            complaint.id, ComplaintStatus.RESOLVED, actor=staff, remarks="Glass pane replaced."
        )

        successes: list[int] = []
        conflicts: list[str] = []

        # Synchronize two callers so both read the same version before either writes
        barrier = threading.Barrier(2)
        original_get = self.repo.get

        def synchronized_get(cid: str):
            res = original_get(cid)
            barrier.wait()
            return res

        self.repo.get = synchronized_get  # type: ignore

        def submit_feedback_worker(rating: int, comment: str) -> None:
            try:
                self.service.submit_feedback(
                    complaint.id,
                    rating=rating,
                    actor=student,
                    comment=comment,
                )
                successes.append(rating)
            except OptimisticLockError as e:
                conflicts.append(type(e).__name__)

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                f1 = executor.submit(submit_feedback_worker, 5, "Rating 5: Excellent work!")
                f2 = executor.submit(submit_feedback_worker, 4, "Rating 4: Good work.")
                f1.result()
                f2.result()
        finally:
            self.repo.get = original_get

        # Exactly one update succeeded under optimistic concurrency, other conflicted
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0], "OptimisticLockError")

        persisted = self.service.get_complaint(complaint.id)
        self.assertIsNotNone(persisted.feedback)
        self.assertEqual(persisted.feedback.rating, successes[0])

    def test_concurrent_staff_assignments_protected_by_optimistic_locking(self):
        """Concurrent staff assignments on the same complaint are protected by optimistic concurrency."""
        student = Actor.student("stu-assign-test")
        admin = Actor.admin("admin-assigner")

        complaint = self.service.create_complaint(
            title="Corridor water dispenser leak",
            description="Continuous stream of clean water leaking onto marble floor.",
            building="Block C",
            floor="1st",
            room_number="Corridor",
            category=IssueCategory.PLUMBING,
            actor=student,
        )

        successes: list[str] = []
        conflicts: list[str] = []

        # Synchronize two callers so both read the same version before either writes
        barrier = threading.Barrier(2)
        original_get = self.repo.get

        def synchronized_get(cid: str):
            res = original_get(cid)
            barrier.wait()
            return res

        self.repo.get = synchronized_get  # type: ignore

        def assign_worker(staff_name: str) -> None:
            try:
                self.service.assign_staff(
                    complaint.id,
                    staff_name=staff_name,
                    actor=admin,
                )
                successes.append(staff_name)
            except OptimisticLockError as e:
                conflicts.append(type(e).__name__)

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                f1 = executor.submit(assign_worker, "Ramesh Technician")
                f2 = executor.submit(assign_worker, "Suresh Technician")
                f1.result()
                f2.result()
        finally:
            self.repo.get = original_get

        # Exactly one assignment succeeded under optimistic concurrency, other conflicted
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0], "OptimisticLockError")

        persisted = self.service.get_complaint(complaint.id)
        self.assertEqual(persisted.assigned_staff, successes[0])


if __name__ == "__main__":
    unittest.main()
