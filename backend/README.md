# MAIT CampusFix — Python Backend

Serverless-ready Python backend (`>= 3.10`) for **MAIT CampusFix**, implementing domain rules, role/ownership authorization, HTTP API v2 request routing, Cognito JWT middleware & Lambda triggers, S3 versioned conditional-copy upload safety, and dual repository backends (`InMemoryComplaintRepository` default for local testing + `DynamoDBComplaintRepository` for offline Stubber/Moto verification and future AWS deployment).

---

## Package Structure

```
backend/
├── pyproject.toml                   # Package metadata & pinned boto3/botocore (1.44.11)
├── README.md
├── src/campusfix/
│   ├── bootstrap.py                 # create_application() wiring shared repository + services + state adapter
│   ├── middleware.py                # Verified Cognito Access Token parsing & fail-closed Role extraction
│   ├── domain/
│   │   ├── models.py                # Complaint, Actor, Role, UploadIntent, UploadAttempt, TagOutboxRecord, UTC helpers
│   │   └── rules.py                 # State transitions, SLAs (3d Fire Safety / 7d std), validation, exception hierarchy
│   ├── repositories/
│   │   ├── interfaces.py            # ComplaintRepository & UploadIntentRepository ABCs
│   │   ├── memory.py                # Default thread-safe InMemoryComplaintRepository
│   │   ├── dynamodb.py              # Single-table DynamoDBComplaintRepository (OCC, GSI1/GSI2, TransactWriteItems, HMAC cursors)
│   │   └── factory.py               # Controlled backend selection (CAMPUSFIX_REPOSITORY_BACKEND, DYNAMODB_TABLE_NAME, AWS_REGION)
│   ├── services/
│   │   ├── complaints.py            # ComplaintService (lifecycle, staff assignment, upvotes, feedback, atomic follow-ups)
│   │   ├── uploads.py               # UploadIntentService (legacy presigned POST/GET + confirmation commit flow)
│   │   ├── s3_storage.py            # S3StorageService ABC + InMemoryS3StorageService
│   │   ├── s3_capabilities.py       # Startup/pre-copy Botocore CopyObjectRequest.IfNoneMatch introspection gate
│   │   ├── boto3_s3_storage.py      # Version-aware Boto3S3StorageService (IfNoneMatch="*", pinned GetObjectVersion, 409/412 checks)
│   │   ├── upload_confirmation_worker.py # Conditional-copy upload confirmation worker with 409 retry & 412 recovery
│   │   ├── repository_state_adapter.py   # RepositoryVersionedUploadStateAdapter bridging worker ports to repositories
│   │   └── id_generator.py          # UUIDYearIdGenerator (CF-{year}-{32hex}) & SequentialYearIdGenerator
│   ├── api/
│   │   ├── dto.py                   # Complaint DTO serialization
│   │   ├── handler.py               # create_api_handler() & create_api_handler_from_env() (13 HTTP API v2 routes + RFC 7807 errors)
│   │   ├── response.py              # CORS allowlist & JSON/Problem response builders
│   │   └── router.py                # HTTP method + path matcher
│   └── triggers/
│       ├── pre_signup.py            # Cognito Pre-Sign-up institutional email domain allowlist Lambda trigger
│       └── post_confirmation.py     # Cognito Post-Confirmation student group enrollment Lambda trigger
└── tests/                           # 18 unittest modules (374 tests)
```

---

## Configuration & Repository Selection

By default (no environment variables set), `create_repository()` and `create_application()` select **`InMemoryComplaintRepository`** with zero AWS SDK client creation or network calls.

| Variable | Default / Requirement | Purpose |
|---|---|---|
| `CAMPUSFIX_REPOSITORY_BACKEND` | Unset -> `memory`; accepts `memory` or `dynamodb` (blank/unknown fails closed with `ValidationError`) | Selects repository adapter |
| `DYNAMODB_TABLE_NAME` | Required non-empty when `dynamodb` is selected | Single-table DynamoDB name |
| `AWS_REGION` | `ap-southeast-2` | AWS region for DynamoDB / S3 config (rejects blank if explicitly set) |
| `PAGINATION_HMAC_SECRET` | Optional in local tests; recommended stable secret in Lambda env | HMAC-SHA256 signing key for DynamoDB pagination cursors |
| `S3_ATTACHMENTS_BUCKET` | `campusfix-attachments-ap-southeast-2` | S3 bucket name for `UploadIntentService` |
| `ALLOWED_STUDENT_EMAIL_DOMAINS` | `cst.mait.ac.in` | Pre-Sign-up trigger student allowlist |
| `ALLOWED_STAFF_EMAIL_DOMAINS` | `mait.ac.in` | Pre-Sign-up trigger staff allowlist |

---

## Testing & Simulated vs. Live AWS Status

**Important:** **Zero live AWS calls or deployments have been executed.** All AWS-backed components are verified offline using Botocore `Stubber`, in-memory doubles, and `moto` (`@mock_aws`).

### Running Tests

```powershell
# From repository root (MSYS2 / workspace venv — 374 discovered, 11 moto tests skipped if moto is not installed)
$env:PYTHONPATH = "backend\src"
.\backend\.venv\bin\python.exe -m unittest discover -s backend/tests -p "test_*.py" -v
```

### Test Modules (374 Total Tests Across 18 Files)

- `test_app_bootstrap.py` (9 tests: factory selection, missing/invalid config rejection, shared-instance wiring, legacy upload path, Moto DynamoDB end-to-end API serving)
- `test_auth_middleware.py` (52 tests: API Gateway JWT claims parsing, `token_use=="access"` enforcement, mutually exclusive `cognito:groups` role mapping)
- `test_authorization.py` (20 tests: server-side actor permissions, anti-coercion, ownership checks)
- `test_boto3_s3_storage.py` (5 tests: versioned S3 operations & Moto integration)
- `test_cognito_triggers.py` (47 tests: pre-signup institutional domain allowlist & post-confirmation group enrollment)
- `test_complaint_rules.py` (14 tests: forward-only state transitions, SLA, remarks, feedback, follow-up eligibility)
- `test_complaint_service.py` (12 tests: service lifecycle, follow-up isolation, serialization round-trip)
- `test_concurrency.py` (7 tests: concurrent follow-up lock, OCC status transitions, concurrent upvotes)
- `test_dynamodb_repository.py` (12 tests: Botocore `Stubber` wire contracts, atomic 2-item `begin_copy_attempt`, Decision 1 cancellation mapping, microsecond lease boundaries, Decision 3 outbox HMAC pagination, plus 5 stateful Moto `@mock_aws` S3+DynamoDB integration tests)
- `test_id_generator.py` (5 tests: UUID4 full-entropy formatting and collision checks)
- `test_input_validation.py` (50 tests: input bounds, MIME/magic bytes, malformed record deserialization)
- `test_repository_safety.py` (17 tests: overwrite protection, OCC versioning, atomic upvote operations)
- `test_router.py` (29 tests: 13 HTTP API v2 routes, RFC 7807 Problem responses, deterministic cursor pagination, CORS)
- `test_sdk_compatibility.py` (7 tests: Botocore `CopyObjectRequest.IfNoneMatch` model introspection gate & pinned SDK check)
- `test_upload_confirmation_worker.py` (21 tests: pinned source validation, `IfNoneMatch="*"` copy, HTTP 409 retry, HTTP 412 cryptographic recovery)
- `test_uploads.py` (31 tests: legacy `UploadIntentService` & `InMemoryS3StorageService` flow)
- `test_upvotes.py` (7 tests: Option B idempotent PUT/DELETE upvote behavior)
- `test_versioned_upload_contracts.py` (29 tests: domain record validation, in-memory parity, and worker-to-adapter integration)

---

## Remaining Backend Work Before AWS Deployment

- Cut over `UploadIntentService` (`uploads.py`) / confirmation path to `Boto3S3StorageService` + `UploadConfirmationWorker` (currently `uploads.py` uses `InMemoryS3StorageService` and legacy `commit_verified_attachment_atomic`).
- Implement the background tag-promotion outbox sweeper (`TagOutboxRecord` -> `s3:PutObjectVersionTagging`).
- Add explicit HTTP error mappings in `handler.py` for `ImmutableFieldViolationError` / `StorageThrottleError`.
- Author deployment infrastructure (e.g. SAM/Terraform for Cognito, API Gateway HTTP API v2, Lambda, DynamoDB table + GSI1/GSI2, versioned S3 bucket) and test against a real AWS staging account.
