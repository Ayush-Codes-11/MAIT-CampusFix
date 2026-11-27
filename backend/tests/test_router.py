"""Unit tests for API Gateway HTTP API v2 router, DTO serialization, and Lambda handler."""

import base64
from datetime import datetime, timezone
import hashlib
import json
from typing import Any
import unittest

from campusfix.api.handler import create_api_handler
from campusfix.domain.models import (
    Actor,
    Complaint,
    ComplaintStatus,
    IssueCategory,
    Role,
)
from campusfix.repositories.memory import InMemoryComplaintRepository
from campusfix.services.complaints import ComplaintService
from campusfix.services.s3_storage import InMemoryS3StorageService
from campusfix.services.uploads import UploadIntentService


def make_event(
    method: str,
    path: str,
    body: Any = None,
    user_id: str | None = "student-101",
    role_group: str | None = "students",
    query_params: dict[str, str] | None = None,
    token_use: str = "access",
    is_base64: bool = False,
    include_auth: bool = True,
    origin: str | None = None,
) -> dict[str, Any]:
    """Helper to synthesize AWS API Gateway HTTP API v2 invocation events."""
    event: dict[str, Any] = {
        "version": "2.0",
        "routeKey": f"{method.upper()} {path}",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {
            "content-type": "application/json",
            "host": "api.campusfix.mait.ac.in",
        },
        "requestContext": {
            "http": {
                "method": method.upper(),
                "path": path,
                "protocol": "HTTP/1.1",
                "sourceIp": "127.0.0.1",
                "userAgent": "pytest-client",
            },
        },
        "isBase64Encoded": is_base64,
    }

    if origin is not None:
        event["headers"]["origin"] = origin

    if query_params:
        event["queryStringParameters"] = query_params

    if include_auth and user_id is not None:
        claims: dict[str, Any] = {
            "sub": user_id,
            "token_use": token_use,
        }
        if role_group is not None:
            claims["cognito:groups"] = [role_group]
        event["requestContext"]["authorizer"] = {
            "jwt": {
                "claims": claims,
            }
        }

    if body is not None:
        if isinstance(body, (dict, list)):
            body_str = json.dumps(body)
        else:
            body_str = str(body)

        if is_base64:
            event["body"] = base64.b64encode(body_str.encode("utf-8")).decode("ascii")
        else:
            event["body"] = body_str

    return event


class TestApiRouterAndHandler(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = InMemoryComplaintRepository()
        self.complaints_service = ComplaintService(self.repo)
        self.s3 = InMemoryS3StorageService(region="ap-southeast-2")
        self.bucket = "test-attachments-bucket"
        self.uploads_service = UploadIntentService(
            complaint_repository=self.repo,
            intent_repository=self.repo,
            s3_storage=self.s3,
            bucket_name=self.bucket,
            region="ap-southeast-2",
        )
        self.handler = create_api_handler(
            complaints_service=self.complaints_service,
            uploads_service=self.uploads_service,
        )

        self.student_id = "student-101"
        self.other_student_id = "student-102"
        self.maintenance_id = "maint-tech-01"
        self.admin_id = "admin-user-01"

        # Seed baseline complaint
        self.complaint = Complaint(
            id="CF-2026-ROUTER01",
            title="Broken projector in Room 301",
            description="Projector does not power on during lectures.",
            building="Academic Block 1",
            floor="3rd Floor",
            room_number="301",
            category=IssueCategory.ELECTRICAL,
            status=ComplaintStatus.PENDING,
            submitted_at=datetime.now(timezone.utc),
            submitted_by=self.student_id,
            upvotes=[],
            version=1,
        )
        self.repo.seed(self.complaint)

    # --- Preflight, CORS & Health Check Tests ---

    def test_cors_allowed_development_origins(self) -> None:
        allowed_dev_origins = [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:4173",
            "http://127.0.0.1:4173",
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ]
        for origin in allowed_dev_origins:
            with self.subTest(origin=origin):
                event = make_event("GET", "/v1/health", include_auth=False, origin=origin)
                response = self.handler(event, None)
                self.assertEqual(response["statusCode"], 200)
                headers = {k.lower(): v for k, v in response.get("headers", {}).items()}
                self.assertEqual(headers.get("access-control-allow-origin"), origin)
                self.assertEqual(headers.get("vary"), "Origin")
                self.assertIn("GET", headers.get("access-control-allow-methods", ""))
                self.assertIn("authorization", headers.get("access-control-allow-headers", "").lower())

    def test_cors_untrusted_origin(self) -> None:
        event = make_event("GET", "/v1/health", include_auth=False, origin="https://attacker.example.com")
        response = self.handler(event, None)
        self.assertEqual(response["statusCode"], 200)
        headers = {k.lower(): v for k, v in response.get("headers", {}).items()}
        self.assertNotIn("access-control-allow-origin", headers)
        self.assertEqual(headers.get("vary"), "Origin")

    def test_cors_missing_origin(self) -> None:
        event = make_event("GET", "/v1/health", include_auth=False, origin=None)
        response = self.handler(event, None)
        self.assertEqual(response["statusCode"], 200)
        headers = {k.lower(): v for k, v in response.get("headers", {}).items()}
        self.assertNotIn("access-control-allow-origin", headers)
        self.assertEqual(headers.get("vary"), "Origin")

    def test_cors_options_preflight_allowed_origin(self) -> None:
        event = make_event("OPTIONS", "/v1/complaints", include_auth=False, origin="http://localhost:5173")
        response = self.handler(event, None)
        self.assertEqual(response["statusCode"], 204)
        headers = {k.lower(): v for k, v in response.get("headers", {}).items()}
        self.assertEqual(headers.get("access-control-allow-origin"), "http://localhost:5173")
        self.assertEqual(headers.get("vary"), "Origin")
        self.assertIn("POST", headers.get("access-control-allow-methods", ""))
        self.assertIn("authorization", headers.get("access-control-allow-headers", "").lower())
        self.assertIn("content-type", headers.get("access-control-allow-headers", "").lower())
        self.assertIn("idempotency-key", headers.get("access-control-allow-headers", "").lower())

    def test_cors_options_preflight_untrusted_origin(self) -> None:
        event = make_event("OPTIONS", "/v1/complaints", include_auth=False, origin="https://malicious-site.com")
        response = self.handler(event, None)
        self.assertEqual(response["statusCode"], 204)
        headers = {k.lower(): v for k, v in response.get("headers", {}).items()}
        self.assertNotIn("access-control-allow-origin", headers)
        self.assertEqual(headers.get("vary"), "Origin")

    def test_health_check_public(self) -> None:
        event = make_event("GET", "/v1/health", include_auth=False)
        response = self.handler(event, None)
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body.get("status"), "healthy")

    # --- Authentication & Authorization Tests ---

    def test_unauthenticated_request_missing_authorizer(self) -> None:
        event = make_event("GET", "/v1/complaints", include_auth=False)
        response = self.handler(event, None)
        self.assertEqual(response["statusCode"], 401)
        body = json.loads(response["body"])
        self.assertEqual(body.get("title"), "Unauthorized")
        self.assertEqual(body.get("code"), "UNAUTHORIZED")

    def test_unauthenticated_request_id_token_rejected(self) -> None:
        event = make_event("GET", "/v1/complaints", token_use="id")
        response = self.handler(event, None)
        self.assertEqual(response["statusCode"], 401)
        body = json.loads(response["body"])
        self.assertEqual(body.get("title"), "Unauthorized")

    def test_forbidden_missing_groups(self) -> None:
        event = make_event("GET", "/v1/complaints", role_group=None)
        response = self.handler(event, None)
        self.assertEqual(response["statusCode"], 403)
        body = json.loads(response["body"])
        self.assertEqual(body.get("title"), "Forbidden")
        self.assertEqual(body.get("code"), "FORBIDDEN")

    def test_forbidden_unknown_group(self) -> None:
        event = make_event("GET", "/v1/complaints", role_group="guests")
        response = self.handler(event, None)
        self.assertEqual(response["statusCode"], 403)

    # --- Request Body Parsing & Validation Tests ---

    def test_malformed_json_body(self) -> None:
        event = make_event("POST", "/v1/complaints", body="INVALID_JSON{")
        response = self.handler(event, None)
        self.assertEqual(response["statusCode"], 400)
        body = json.loads(response["body"])
        self.assertEqual(body.get("title"), "Bad Request")
        self.assertEqual(body.get("code"), "INVALID_REQUEST_BODY")

    def test_base64_encoded_body_success(self) -> None:
        payload = {
            "title": "Leaking faucet in restroom",
            "description": "Continuous dripping from faucet on 2nd floor restroom.",
            "building": "Admin Block",
            "floor": "2nd Floor",
            "roomNumber": "Restroom 2B",
            "category": "Plumbing",
        }
        event = make_event("POST", "/v1/complaints", body=payload, is_base64=True)
        response = self.handler(event, None)
        self.assertEqual(response["statusCode"], 201)
        body = json.loads(response["body"])
        self.assertEqual(body["title"], payload["title"])
        self.assertEqual(body["building"], payload["building"])

    def test_route_not_found(self) -> None:
        event = make_event("GET", "/v1/unknown-endpoint")
        response = self.handler(event, None)
        self.assertEqual(response["statusCode"], 404)
        body = json.loads(response["body"])
        self.assertEqual(body.get("code"), "ROUTE_NOT_FOUND")

    # --- Complaint Endpoints & DTO Verification ---

    def test_list_complaints_dto_and_pii_masking(self) -> None:
        # Request as other student
        event = make_event("GET", "/v1/complaints", user_id=self.other_student_id, role_group="students")
        response = self.handler(event, None)
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertIn("items", body)
        self.assertGreaterEqual(len(body["items"]), 1)

        item = body["items"][0]
        # PII Masking: for other student, submittedBy is None (masked)
        self.assertIsNone(item["submittedBy"])
        self.assertFalse(item["isOwner"])
        self.assertFalse(item["hasUpvoted"])
        self.assertNotIn("upvotedBy", item)

        # Request as owner student: unmasked
        event_owner = make_event("GET", "/v1/complaints", user_id=self.student_id, role_group="students")
        res_owner = self.handler(event_owner, None)
        body_owner = json.loads(res_owner["body"])
        item_owner = body_owner["items"][0]
        self.assertEqual(item_owner["submittedBy"], self.student_id)
        self.assertTrue(item_owner["isOwner"])

        # Request as staff: unmasked
        event_staff = make_event("GET", "/v1/complaints", user_id=self.maintenance_id, role_group="maintenance")
        res_staff = self.handler(event_staff, None)
        body_staff = json.loads(res_staff["body"])
        item_staff = body_staff["items"][0]
        self.assertEqual(item_staff["submittedBy"], self.student_id)
        self.assertFalse(item_staff["isOwner"])

    def test_list_complaints_building_filter(self) -> None:
        # Create second complaint in distinct building
        cmp_b = Complaint(
            id="CF-2026-BUILDING02",
            title="Leaking pipe in Library",
            description="Continuous water dripping in basement.",
            building="Library Block",
            floor="Basement",
            room_number="B-01",
            category=IssueCategory.PLUMBING,
            status=ComplaintStatus.PENDING,
            submitted_at=datetime.now(timezone.utc),
            submitted_by=self.student_id,
            upvotes=[],
            version=1,
        )
        self.repo.seed(cmp_b)

        # Filter by building
        event = make_event(
            "GET",
            "/v1/complaints",
            query_params={"building": "Library Block"},
            user_id=self.student_id,
            role_group="students",
        )
        response = self.handler(event, None)
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["building"], "Library Block")

    def test_deterministic_pagination_ordering_with_identical_timestamps(self) -> None:
        fixed_time = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc)
        test_ids = ["CF-ORDER-A", "CF-ORDER-B", "CF-ORDER-C", "CF-ORDER-D"]

        for cid in test_ids:
            c = Complaint(
                id=cid,
                title=f"Test title {cid}",
                description="Test description for pagination ordering verification.",
                building="Order Block",
                floor="1st Floor",
                room_number="101",
                category=IssueCategory.ELECTRICAL,
                status=ComplaintStatus.PENDING,
                submitted_at=fixed_time,
                submitted_by=self.student_id,
                upvotes=[],
                version=1,
            )
            self.repo.seed(c)

        expected_descending_order = sorted(test_ids, reverse=True)  # D, C, B, A

        # 1. Verify repeated calls return identical deterministic ordering
        for _ in range(3):
            event = make_event(
                "GET",
                "/v1/complaints",
                query_params={"building": "Order Block", "limit": "10"},
                user_id=self.student_id,
                role_group="students",
            )
            res = self.handler(event, None)
            self.assertEqual(res["statusCode"], 200)
            body = json.loads(res["body"])
            actual_ids = [item["id"] for item in body["items"]]
            self.assertEqual(actual_ids, expected_descending_order)

        # 2. Verify paginated navigation without duplication or omissions
        # Page 1 (limit 2)
        event_p1 = make_event(
            "GET",
            "/v1/complaints",
            query_params={"building": "Order Block", "limit": "2"},
            user_id=self.student_id,
            role_group="students",
        )
        res_p1 = self.handler(event_p1, None)
        body_p1 = json.loads(res_p1["body"])
        self.assertEqual(len(body_p1["items"]), 2)
        self.assertEqual([item["id"] for item in body_p1["items"]], expected_descending_order[:2])
        self.assertIsNotNone(body_p1["nextCursor"])

        # Page 2 (using nextCursor)
        event_p2 = make_event(
            "GET",
            "/v1/complaints",
            query_params={
                "building": "Order Block",
                "limit": "2",
                "cursor": body_p1["nextCursor"],
            },
            user_id=self.student_id,
            role_group="students",
        )
        res_p2 = self.handler(event_p2, None)
        body_p2 = json.loads(res_p2["body"])
        self.assertEqual(len(body_p2["items"]), 2)
        self.assertEqual([item["id"] for item in body_p2["items"]], expected_descending_order[2:])
        self.assertIsNone(body_p2["nextCursor"])

        # Combined check: no duplicates across pages
        all_paginated_ids = [item["id"] for item in body_p1["items"]] + [item["id"] for item in body_p2["items"]]
        self.assertEqual(all_paginated_ids, expected_descending_order)
        self.assertEqual(len(set(all_paginated_ids)), 4)

    def test_create_complaint_student_success(self) -> None:
        payload = {
            "title": "Broken bench in hallway",
            "description": "Wooden bench has a cracked leg.",
            "building": "Library Block",
            "floor": "Ground Floor",
            "roomNumber": "Hallway L-1",
            "category": "Furniture",
        }
        event = make_event("POST", "/v1/complaints", body=payload, user_id=self.student_id, role_group="students")
        response = self.handler(event, None)
        self.assertEqual(response["statusCode"], 201)
        body = json.loads(response["body"])
        self.assertEqual(body["title"], payload["title"])
        self.assertEqual(body["status"], "pending")
        self.assertEqual(body["submittedBy"], self.student_id)
        self.assertTrue(body["isOwner"])

    def test_create_complaint_non_student_forbidden(self) -> None:
        payload = {
            "title": "Broken bench in hallway",
            "description": "Wooden bench has a cracked leg.",
            "building": "Library Block",
            "floor": "Ground Floor",
            "roomNumber": "Hallway L-1",
            "category": "Furniture",
        }
        event = make_event("POST", "/v1/complaints", body=payload, user_id=self.maintenance_id, role_group="maintenance")
        response = self.handler(event, None)
        self.assertEqual(response["statusCode"], 403)

    def test_get_complaint_by_id_success_and_not_found(self) -> None:
        event = make_event("GET", f"/v1/complaints/{self.complaint.id}", user_id=self.student_id)
        response = self.handler(event, None)
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["id"], self.complaint.id)
        self.assertTrue(body["isOwner"])

        event_missing = make_event("GET", "/v1/complaints/CF-NONEXISTENT", user_id=self.student_id)
        res_missing = self.handler(event_missing, None)
        self.assertEqual(res_missing["statusCode"], 404)
        body_missing = json.loads(res_missing["body"])
        self.assertEqual(body_missing.get("code"), "RESOURCE_NOT_FOUND")

    # --- Status Transition & Staff Assignment Tests ---

    def test_patch_status_maintenance_success_and_occ(self) -> None:
        payload = {
            "status": "in-progress",
            "remarks": "Assigned technician visiting site.",
            "expectedVersion": 1,
        }
        event = make_event(
            "PATCH",
            f"/v1/complaints/{self.complaint.id}/status",
            body=payload,
            user_id=self.maintenance_id,
            role_group="maintenance",
        )
        response = self.handler(event, None)
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["status"], "in-progress")
        self.assertEqual(body["version"], 2)

        # Concurrent update with stale expectedVersion 1 should return 409 Conflict
        stale_payload = {
            "status": "resolved",
            "remarks": "Fixed the issue.",
            "expectedVersion": 1,
        }
        stale_event = make_event(
            "PATCH",
            f"/v1/complaints/{self.complaint.id}/status",
            body=stale_payload,
            user_id=self.maintenance_id,
            role_group="maintenance",
        )
        stale_res = self.handler(stale_event, None)
        self.assertEqual(stale_res["statusCode"], 409)
        stale_body = json.loads(stale_res["body"])
        self.assertEqual(stale_body.get("code"), "CONFLICT")

    def test_patch_status_student_forbidden(self) -> None:
        payload = {"status": "in-progress", "remarks": "Trying to change status"}
        event = make_event(
            "PATCH",
            f"/v1/complaints/{self.complaint.id}/status",
            body=payload,
            user_id=self.student_id,
            role_group="students",
        )
        response = self.handler(event, None)
        self.assertEqual(response["statusCode"], 403)

    def test_patch_staff_admin_success(self) -> None:
        payload = {"assignedStaff": "Ramesh Kumar (Electrician)", "expectedVersion": 1}
        event = make_event(
            "PATCH",
            f"/v1/complaints/{self.complaint.id}/staff",
            body=payload,
            user_id=self.admin_id,
            role_group="admins",
        )
        response = self.handler(event, None)
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["assignedStaff"], "Ramesh Kumar (Electrician)")

    # --- Upvote Tests ---

    def test_put_and_delete_upvote_lifecycle(self) -> None:
        # Student upvotes
        event_put = make_event(
            "PUT",
            f"/v1/complaints/{self.complaint.id}/upvote",
            user_id=self.other_student_id,
            role_group="students",
        )
        res_put = self.handler(event_put, None)
        self.assertEqual(res_put["statusCode"], 200)
        body_put = json.loads(res_put["body"])
        self.assertEqual(body_put["upvotes"], 1)
        self.assertTrue(body_put["hasUpvoted"])

        # Repeat upvote (idempotent)
        res_repeat = self.handler(event_put, None)
        self.assertEqual(res_repeat["statusCode"], 200)
        body_repeat = json.loads(res_repeat["body"])
        self.assertEqual(body_repeat["upvotes"], 1)

        # Student removes upvote
        event_del = make_event(
            "DELETE",
            f"/v1/complaints/{self.complaint.id}/upvote",
            user_id=self.other_student_id,
            role_group="students",
        )
        res_del = self.handler(event_del, None)
        self.assertEqual(res_del["statusCode"], 200)
        body_del = json.loads(res_del["body"])
        self.assertEqual(body_del["upvotes"], 0)
        self.assertFalse(body_del["hasUpvoted"])

    def test_upvote_non_student_forbidden(self) -> None:
        event = make_event(
            "PUT",
            f"/v1/complaints/{self.complaint.id}/upvote",
            user_id=self.maintenance_id,
            role_group="maintenance",
        )
        response = self.handler(event, None)
        self.assertEqual(response["statusCode"], 403)

    # --- Feedback & Follow-Up Endpoints ---

    def test_post_feedback_success_and_restrictions(self) -> None:
        # First resolve the complaint
        self.complaints_service.transition_status(
            complaint_id=self.complaint.id,
            to_status="in-progress",
            actor=Actor(self.maintenance_id, Role.MAINTENANCE),
            remarks="Investigating",
            expected_version=1,
        )
        self.complaints_service.transition_status(
            complaint_id=self.complaint.id,
            to_status="resolved",
            actor=Actor(self.maintenance_id, Role.MAINTENANCE),
            remarks="Replaced faulty cable.",
            expected_version=2,
        )

        # Owner submits feedback
        feedback_payload = {"rating": 5, "comment": "Great quick service!"}
        event = make_event(
            "POST",
            f"/v1/complaints/{self.complaint.id}/feedback",
            body=feedback_payload,
            user_id=self.student_id,
            role_group="students",
        )
        response = self.handler(event, None)
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertIsNotNone(body.get("feedback"))
        self.assertEqual(body["feedback"]["rating"], 5)

        # Non-owner cannot submit feedback
        event_other = make_event(
            "POST",
            f"/v1/complaints/{self.complaint.id}/feedback",
            body=feedback_payload,
            user_id=self.other_student_id,
            role_group="students",
        )
        res_other = self.handler(event_other, None)
        self.assertEqual(res_other["statusCode"], 403)

    def test_post_follow_up_and_list_follow_ups(self) -> None:
        # Move complaint to resolved
        self.complaints_service.transition_status(
            complaint_id=self.complaint.id,
            to_status="in-progress",
            actor=Actor(self.maintenance_id, Role.MAINTENANCE),
            remarks="Investigating",
            expected_version=1,
        )
        self.complaints_service.transition_status(
            complaint_id=self.complaint.id,
            to_status="resolved",
            actor=Actor(self.maintenance_id, Role.MAINTENANCE),
            remarks="Fixed",
            expected_version=2,
        )

        follow_up_payload = {
            "reason": "Projector works intermittently, flickering screen.",
            "title": "Flickering issue persists on projector",
        }
        event = make_event(
            "POST",
            f"/v1/complaints/{self.complaint.id}/follow-ups",
            body=follow_up_payload,
            user_id=self.student_id,
            role_group="students",
        )
        response = self.handler(event, None)
        self.assertEqual(response["statusCode"], 201)
        body = json.loads(response["body"])
        self.assertEqual(body["originalComplaintId"], self.complaint.id)
        self.assertEqual(body["title"], follow_up_payload["title"])

        # Fetch linked follow-ups
        get_event = make_event("GET", f"/v1/complaints/{self.complaint.id}/follow-ups", user_id=self.student_id)
        get_res = self.handler(get_event, None)
        self.assertEqual(get_res["statusCode"], 200)
        get_body = json.loads(get_res["body"])
        self.assertEqual(len(get_body), 1)
        self.assertEqual(get_body[0]["originalComplaintId"], self.complaint.id)

    def test_post_follow_up_rejected_on_unresolved_complaint(self) -> None:
        """Verify that follow-ups are strictly forbidden for pending/in-progress complaints even if overdue."""
        # Complaint is in PENDING status
        follow_up_payload = {
            "reason": "Attempting follow-up on an unresolved complaint.",
        }
        event = make_event(
            "POST",
            f"/v1/complaints/{self.complaint.id}/follow-ups",
            body=follow_up_payload,
            user_id=self.student_id,
            role_group="students",
        )
        response = self.handler(event, None)
        self.assertEqual(response["statusCode"], 400)
        body = json.loads(response["body"])
        self.assertEqual(body.get("code"), "VALIDATION_ERROR")
        self.assertIn("Follow-ups are permitted only for Resolved complaints", body.get("detail", ""))

    # --- S3 Upload Endpoints ---

    def test_upload_intent_and_confirmation_routes(self) -> None:
        # 1. Request upload intent
        jpeg_payload = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 1500
        sha256_hex = hashlib.sha256(jpeg_payload).hexdigest()

        intent_payload = {
            "contentType": "image/jpeg",
            "byteSize": len(jpeg_payload),
            "checksumSha256": sha256_hex,
        }
        event_intent = make_event(
            "POST",
            f"/v1/complaints/{self.complaint.id}/images/upload-intent",
            body=intent_payload,
            user_id=self.student_id,
            role_group="students",
        )
        res_intent = self.handler(event_intent, None)
        self.assertEqual(res_intent["statusCode"], 200)
        body_intent = json.loads(res_intent["body"])
        intent_id = body_intent["intentId"]
        self.assertIn("uploadUrl", body_intent)

        # 2. Simulate staging upload in S3
        intent = self.repo.get_intent(self.complaint.id, intent_id)
        self.assertIsNotNone(intent)
        self.s3.put_object(self.bucket, intent.staging_key, jpeg_payload, "image/jpeg")

        # 3. Confirm upload
        confirm_payload = {"intentId": intent_id}
        event_confirm = make_event(
            "POST",
            f"/v1/complaints/{self.complaint.id}/images/confirm",
            body=confirm_payload,
            user_id=self.student_id,
            role_group="students",
        )
        res_confirm = self.handler(event_confirm, None)
        self.assertEqual(res_confirm["statusCode"], 200)
        body_confirm = json.loads(res_confirm["body"])
        self.assertEqual(body_confirm["status"], "VERIFIED")
        self.assertIn("attachmentKey", body_confirm)

        # 4. Request download URL
        event_dl = make_event(
            "GET",
            f"/v1/complaints/{self.complaint.id}/images/download-url",
            user_id=self.student_id,
            role_group="students",
        )
        res_dl = self.handler(event_dl, None)
        self.assertEqual(res_dl["statusCode"], 200)
        body_dl = json.loads(res_dl["body"])
        self.assertIn("downloadUrl", body_dl)
        self.assertIn("expiresAt", body_dl)

    # --- Internal Error Sanitization ---

    def test_unexpected_internal_error_sanitization(self) -> None:
        # Patch service method to raise an unhandled RuntimeError
        def faulty_method(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("Database connection string leaked: secret_pwd@10.0.0.1")

        self.complaints_service.get_complaint_or_raise = faulty_method  # type: ignore

        event = make_event("GET", f"/v1/complaints/{self.complaint.id}", user_id=self.student_id)
        response = self.handler(event, None)
        self.assertEqual(response["statusCode"], 500)
        body = json.loads(response["body"])
        self.assertEqual(body.get("title"), "Internal Server Error")
        self.assertEqual(body.get("code"), "INTERNAL_SERVER_ERROR")
        # Ensure raw secret is not leaked in response body
        self.assertNotIn("secret_pwd", response["body"])


if __name__ == "__main__":
    unittest.main()
