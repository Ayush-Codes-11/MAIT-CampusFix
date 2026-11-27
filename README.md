# MAIT CampusFix

> **Campus Maintenance Complaint Reporting & Accountability Platform**  
> Designed for Maharaja Agrasen Institute of Technology (MAIT), Delhi

---

## Status & Truthful Testing Notice

MAIT CampusFix is currently a **local, un-deployed prototype** with a React UI (using browser `localStorage` + local mock authentication) and a tested Python serverless backend + AWS adapter layer.

- **No live AWS resources are provisioned or deployed.**
- **No live AWS API calls have been executed.** All DynamoDB and S3 integration tests run strictly locally against `InMemoryComplaintRepository`, `InMemoryS3StorageService`, Botocore `Stubber`, and `moto` (`@mock_aws`) local mocks.
- **Seed data is illustrative only** (`src/data/mockData.ts`) and does **not** represent real MAIT complaint statistics.
- **Frontend UI pages currently persist in browser `localStorage`** (`src/utils/storage.ts`) and are not yet wired to call the Python API Gateway/Lambda backend.

---

## 1. The Problem CampusFix Addresses

Campus maintenance issues (faulty electrical fixtures, plumbing leaks, network dead zones, furniture hazards, fire safety defects) are frequently reported ad hoc via informal channels without:

- Shared visibility or deduplication, causing multiple students to report the same classroom issue independently
- Structured accountability (staff assignment, forward-only status history, resolution remarks)
- SLA monitoring for safety-critical issues such as missing or expired fire extinguishers
- Verifiable student feedback or structured re-opening when a fix fails

CampusFix solves this with structured complaint tracking, priority signaling via student upvotes, SLA-driven overdue detection, single-active follow-up tickets after resolution, and a fail-closed serverless backend design prepared for AWS deployment.

---

## 2. Core Features & User Workflow

### User Roles & Workflow

1. **Student (`student`)**
   - **Dashboard (`/`)**: View campus-wide complaint summary, overdue alerts, building breakdown, and recent activity.
   - **Report an Issue (`/report`)**: Guided complaint form (`Building`, `Floor`, `Room / Area`, `Category`, `Title`, `Description >= 20 chars`, optional photo preview `< 5 MB`). Surfaces open similar complaints in the same building/category as **non-blocking** suggestions and shows an explicit disclaimer confirming submission only (not official MAIT acceptance).
   - **Complaints Feed (`/complaints`)**: Search, filter (`Status`, `Building`, `Category`, `Original vs Follow-up`), sort (`Newest`, `Oldest`, `Most Upvoted`), and toggle upvotes (one vote per student).
   - **Complaint Detail (`/complaints/:id`)**: View timeline history, upvote, rate & review (`1-5` stars + optional comment) on own resolved complaints, or raise a **follow-up complaint** when a resolved issue recurs (strictly one active unresolved follow-up allowed per parent complaint).
2. **Maintenance Staff (`maintenance`)**
   - Automatically redirected from `/` to **`/admin`** (`Staff / Admin Panel`).
   - Search and filter tickets (overdue items surfaced first), assign staff technicians, transition tickets strictly forward (`pending -> in-progress -> resolved`) with mandatory resolution remarks, and inspect status timelines.
3. **Administrator (`admin`)**
   - Full access across student views, staff resolution panel (`/admin`), and **Analytics (`/analytics`)** (resolved vs pending counts, average and median resolution time in days, overdue table, building & category charts).

### Key Domain & Security Invariants

- **Strict SLA Thresholds**: 3-day overdue threshold for `Fire Safety` (cannot be relaxed by user input); 7-day default for standard categories.
- **Forward-Only Workflow**: `pending -> in-progress -> resolved` (terminal; remarks required on `resolved`).
- **One Active Follow-Up Invariant**: Only the original student owner can create a follow-up on a `resolved` ticket, and at most one unresolved follow-up may exist at a time per parent ticket.
- **Fail-Closed Auth**: `configureAuth()` (`src/config/auth.ts`) forbids `VITE_ENABLE_MOCK_AUTH=true` in production builds and rejects missing Cognito config when mock mode is disabled. Backend middleware (`campusfix.middleware`) derives identity/role exclusively from verified Cognito Access Token claims (`token_use == "access"`, `sub`, mutually exclusive `cognito:groups`).

---

## 3. Architecture & Planned AWS Services

| Layer | Local Prototype Implementation | Implemented AWS Target Components (Tested Offline) | Cloud Status |
|---|---|---|---|
| **Frontend SPA** | React 19, Vite 8, TypeScript, Tailwind v4, React Router 7 | Static hosting + AWS Amplify Auth v6 (`src/config/auth.ts`, `src/services/api.ts`) | Built locally (`dist/`); uses `localStorage` |
| **Auth / Identity** | Local `VITE_ENABLE_MOCK_AUTH=true` switcher (`Student`, `Maintenance`, `Admin`) | Amazon Cognito User Pool (`students`, `maintenance`, `admins` groups + Pre-Sign-up/Post-Confirmation Lambda triggers in `backend/src/campusfix/triggers/`) | Not provisioned |
| **API Compute** | `create_api_handler` / `create_api_handler_from_env` (`backend/src/campusfix/api/handler.py`) | AWS Lambda + Amazon API Gateway HTTP API v2 + Cognito JWT Authorizer | Tested locally against synthetic events |
| **Persistence** | `InMemoryComplaintRepository` (default via `create_repository()`) | `DynamoDBComplaintRepository` (`backend/src/campusfix/repositories/dynamodb.py`): single-table `PK`/`SK`, `GSI1` (status + time), `GSI2` (follow-ups), OCC, transactional locks | Tested via Botocore `Stubber` & `moto` |
| **Image Pipeline** | `InMemoryS3StorageService` + `UploadIntentService` | Versioned S3 bucket + `Boto3S3StorageService` (`IfNoneMatch="*"` gate) + `UploadConfirmationWorker` + `RepositoryVersionedUploadStateAdapter` + Outbox records | Worker/adapter tested via `Stubber` & `moto`; S3 bucket & sweeper not live |

---

## 4. What Is Implemented vs. Incomplete

### Implemented & Locally Verified

- **Frontend (`src/`)**: All 6 routed views (`/`, `/report`, `/complaints`, `/complaints/:id`, `/admin`, `/analytics`), role-based route protection (`ProtectedRoute`), fail-closed Amplify/Cognito config & `authenticatedFetch` network helper, seeded `localStorage` store with follow-ups & feedback.
  - Verified locally: `npm run typecheck`, `npm run build` (`vite build` -> `dist/`), and **71 Vitest tests across 6 test files**.
- **Backend (`backend/`)**: Domain models/rules, optimistic concurrency control (OCC), atomic upvote & follow-up lock operations, UUID complaint ID generator, Cognito JWT middleware & email allowlist triggers, 13-route API Gateway HTTP API v2 router/handler, repository selection factory (`CAMPUSFIX_REPOSITORY_BACKEND`), shared application composition (`bootstrap.py`), `DynamoDBComplaintRepository`, and Stage B1 versioned conditional-copy S3 adapter/worker (`boto3==1.44.11`, `botocore==1.44.11`).
  - Verified locally across **18 test files / 374 unittest tests** (using `InMemoryComplaintRepository`, `InMemoryS3StorageService`, Botocore `Stubber`, and `moto` `@mock_aws`). See [`backend/README.md`](backend/README.md) for the complete breakdown.

### What Remains Incomplete (Out of Scope / Pre-Deployment)

1. **Frontend UI ↔ Backend API Integration**: UI pages read/write browser `localStorage` (`src/utils/storage.ts`) rather than calling the Python API handlers via `src/services/api.ts`.
2. **Versioned Upload Service Cutover**: `UploadIntentService` (`backend/src/campusfix/services/uploads.py`) still uses `InMemoryS3StorageService` and the legacy commit contract rather than cutting over to `Boto3S3StorageService` + `UploadConfirmationWorker` (presigned POST/GET methods are not yet unified on one service cutover).
3. **Outbox Tag-Promotion Sweeper**: Outbox records (`TagOutboxRecord`) and CRUD methods are implemented and tested, but the background sweeper executing `s3:PutObjectVersionTagging` is not yet implemented.
4. **API Error Mapping for Stage B2 Exceptions**: `ImmutableFieldViolationError` and `StorageThrottleError` are not yet explicitly mapped to HTTP 409/503 responses in `handler.py`.
5. **AWS Infrastructure & Live Cloud Testing**: No IaC templates (SAM/Terraform), Cognito User Pool, API Gateway, DynamoDB table, or S3 bucket have been deployed or tested against a real AWS account.
6. **Frontend Linter on Windows (`oxlint`)**: In the local verification environment, `npm run lint` is blocked by a Windows Application Control policy on `node_modules/@oxlint/binding-win32-x64-msvc/oxlint.win32-x64-msvc.node` (TypeScript typecheck, Vitest, and Vite production build still pass cleanly).

---

## 5. Quick Start & Demo

See **[`DEMO.md`](DEMO.md)** for the full step-by-step 5-minute demo script using the local frontend and backend verification commands.

### Minimal Local Frontend Run

> **Important:** Because the frontend fails closed when AWS Cognito variables are unconfigured, pass `VITE_ENABLE_MOCK_AUTH=true` in your shell when starting `vite` for local development. Do **not** leave `VITE_ENABLE_MOCK_AUTH=true` in `.env.local` during `npm run build` / `npm run preview`, because production builds intentionally reject mock auth (`src/config/auth.ts`).

**Windows PowerShell:**
```powershell
$env:VITE_ENABLE_MOCK_AUTH = "true"
npm run dev
```

**macOS / Linux / Git Bash:**
```bash
VITE_ENABLE_MOCK_AUTH=true npm run dev
```

Open **http://localhost:5173**. Use the amber **Mock Role** selector in the top bar to switch between **Student**, **Maintenance**, and **Admin**.

### Production Build Check

```powershell
# Ensure VITE_ENABLE_MOCK_AUTH is NOT set when running production builds
Remove-Item Env:VITE_ENABLE_MOCK_AUTH -ErrorAction SilentlyContinue
npm run typecheck
npm run test
npm run build
```

### Local Backend Test Run

```powershell
$env:PYTHONPATH = "backend\src"
.\backend\.venv\bin\python.exe -m unittest discover -s backend/tests -p "test_*.py" -v
```

*(Uses `InMemoryComplaintRepository` by default; no AWS credentials required.)*

---

## Disclaimer

This platform is an academic student initiative for MAIT and is **not** an official MAIT grievance portal.
