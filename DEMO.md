# MAIT CampusFix — Local Demo Guide

This guide walks through a reliable 5-minute demonstration of the working local MAIT CampusFix application without AWS credentials or live cloud resources.

---

## Before You Start

- Ensure dependencies are already installed (`node_modules/` and `backend/.venv/`).
- The frontend runs locally using **browser `localStorage`** (`campusfix_complaints`) seeded automatically with **10 illustrative demo complaints** (`CF-2024-0001` through `CF-2024-0010`) and **Mock Auth mode** (`VITE_ENABLE_MOCK_AUTH=true`).
- **Important:** Set `VITE_ENABLE_MOCK_AUTH=true` only in your dev server session (do not keep it in `.env.local` if you run `npm run build`, as production builds explicitly forbid mock authentication).

---

## Step 1: Launch the Local Frontend

**Windows PowerShell:**
```powershell
$env:VITE_ENABLE_MOCK_AUTH = "true"
npm run dev
```

**macOS / Linux / Git Bash:**
```bash
VITE_ENABLE_MOCK_AUTH=true npm run dev
```

Open **http://localhost:5173** in your browser.

*(To reset demo data at any point, open browser DevTools Console and run:)*
```js
localStorage.removeItem('campusfix_complaints');
localStorage.removeItem('campusfix_mock_auth_role');
location.reload();
```

---

## Step 2: 5-Minute Interactive Walkthrough

### Part A — Student Experience (`Mock Role: Student`)

1. Confirm the amber **Mock Role** dropdown in the top bar is set to **Student** (`Ayush Sharma`, `mock-stu-001`).
2. **Dashboard (`/`)**:
   - Point out total/pending/in-progress/resolved cards, building breakdown, and overdue alerts (e.g., `CF-2024-0001` Lab C-204 lights, `CF-2024-0004` Library Wi-Fi, `CF-2024-0006` Admin seepage).
3. **Report an Issue (`/report`)**:
   - Select **Building**: `Block A`, **Floor**: `1st Floor`, **Room / Area**: `Room A-104`, **Category**: `Plumbing`.
   - Observe the amber **"Similar complaints already exist"** panel (non-blocking suggestion referencing `CF-2024-0002` — students can still submit).
   - Enter a title and a description of at least 20 characters, optionally attach an image `< 5 MB`, and click submit.
   - Show the **Acknowledgement Notice** explicitly clarifying that submission does not constitute official MAIT administrative acceptance.
   - Change **Category** to `Fire Safety` in a second quick example (or explain via seed `CF-2024-0007` Workshop Block) to highlight the enforced **3-day Fire Safety SLA** vs. standard **7-day SLA**.
4. **Complaints Feed (`/complaints`) & Upvoting**:
   - Filter by status (`Pending`), category, or building, and sort by **Most Upvoted**.
   - Click the upvote button on a ticket to show single-vote toggling.
5. **Resolution Feedback & Single-Follow-Up Accountability (`/complaints/CF-2024-0003`)**:
   - Open resolved complaint **`CF-2024-0003`** (*Projector not working in MB-101*, submitted by `mock-stu-001`).
   - Submit a star rating (`1–5`) and comment in the feedback section.
   - Open the **Raise Follow-Up** form to file a follow-up ticket explaining the recurrence (`>= 10` chars). Note that once an active unresolved follow-up exists, a second active follow-up on the same parent complaint is blocked until the first follow-up is resolved.

### Part B — Maintenance Staff Experience (`Mock Role: Maintenance`)

1. In the top bar **Mock Role** selector, switch from **Student** to **Maintenance**.
2. Observe that `/` automatically redirects to **`/admin`** (`Staff / Admin Panel`) and hides **Report an Issue** and **Analytics** from the sidebar (least-privilege RBAC).
3. Select the newly created complaint (or any `Pending` ticket):
   - Assign a staff member (e.g. `Ramesh Kumar`).
   - Transition **`pending -> in-progress`**, then try transitioning to **`resolved`** without remarks to show remark validation, then add resolution remarks and complete transition to **`resolved`**.
   - Expand the **Status Timeline** to verify the immutable audit trail.

### Part C — Administrator Analytics (`Mock Role: Admin`)

1. Switch **Mock Role** to **Admin**.
2. Open **Analytics (`/analytics`)** in the sidebar.
3. Walk through resolved/pending counts, average & median resolution time metrics, overdue complaints table, and building/category breakdowns.

---

## Step 3: Local Backend Verification (No Live AWS Required)

Run the Python backend test suite (defaults cleanly to `InMemoryComplaintRepository`; DynamoDB and S3 tests use offline Botocore `Stubber` and `moto` mocks):

```powershell
# Using workspace venv (374 tests; moto-specific tests skipped if moto is absent)
$env:PYTHONPATH = "backend\src"
.\backend\.venv\bin\python.exe -m unittest discover -s backend/tests -p "test_*.py" -v
```

### Quick Local Smoke Check of Python Composition + API Handler

Verify local composition (`InMemoryComplaintRepository` default, shared services, API Gateway v2 handler) without any AWS account or credentials:

```powershell
$env:PYTHONPATH = "backend\src"
.\backend\.venv\bin\python.exe -c "
import json
from campusfix.api.handler import create_api_handler_from_env

handler = create_api_handler_from_env()
create_res = handler({
    'version': '2.0',
    'rawPath': '/v1/complaints',
    'headers': {'content-type': 'application/json'},
    'requestContext': {
        'http': {'method': 'POST', 'path': '/v1/complaints'},
        'authorizer': {'jwt': {'claims': {'sub': 'demo-stu-1', 'token_use': 'access', 'cognito:groups': ['students']}}},
    },
    'body': json.dumps({
        'title': 'Demo AC fault in Room 204',
        'description': 'Cooling unit in Block A Room 204 stops after 10 minutes.',
        'building': 'Block A',
        'floor': '2nd',
        'roomNumber': '204',
        'category': 'Electrical',
    }),
    'isBase64Encoded': False,
}, None)
print('POST /v1/complaints ->', create_res['statusCode'])
created = json.loads(create_res['body'])
list_res = handler({
    'version': '2.0',
    'rawPath': '/v1/complaints',
    'headers': {},
    'requestContext': {
        'http': {'method': 'GET', 'path': '/v1/complaints'},
        'authorizer': {'jwt': {'claims': {'sub': 'demo-stu-1', 'token_use': 'access', 'cognito:groups': ['students']}}},
    },
    'body': '',
    'isBase64Encoded': False,
}, None)
print('GET /v1/complaints ->', list_res['statusCode'], 'created id:', created['id'])
"
```
