// ─── Core domain types for MAIT CampusFix ───────────────────────────────────
// All data is stored in localStorage for the prototype phase.

export type ComplaintStatus = 'pending' | 'in-progress' | 'resolved';

export type IssueCategory =
  | 'Electrical'
  | 'Plumbing'
  | 'Civil / Structural'
  | 'Housekeeping'
  | 'IT / Network'
  | 'Furniture'
  | 'Fire Safety'
  | 'Other';

export const ISSUE_CATEGORIES: IssueCategory[] = [
  'Electrical',
  'Plumbing',
  'Civil / Structural',
  'Housekeeping',
  'IT / Network',
  'Furniture',
  'Fire Safety',
  'Other',
];

export const BUILDINGS = [
  'Main Block (MB)',
  'Block A',
  'Block B',
  'Block C (Labs)',
  'Library Block',
  'Auditorium',
  'Sports Complex',
  'Administrative Block',
  'Canteen Block',
  'Workshop Block',
];

export const FLOORS = ['Ground Floor', '1st Floor', '2nd Floor', '3rd Floor', '4th Floor'];

// ─── Status workflow ──────────────────────────────────────────────────────────
// Forward-only: pending → in-progress → resolved (terminal)
// Backward transitions and stage-skipping are forbidden.

export const VALID_TRANSITIONS: Record<ComplaintStatus, ComplaintStatus[]> = {
  'pending':     ['in-progress'],   // can only move forward to in-progress
  'in-progress': ['resolved'],      // can only move forward to resolved
  'resolved':    [],                // terminal — no further transitions allowed
};

export const STATUS_LABELS: Record<ComplaintStatus, string> = {
  'pending':     'Pending',
  'in-progress': 'In Progress',
  'resolved':    'Resolved',
};

/** Returns the allowed next statuses for a given current status. */
export function getAllowedNextStatuses(current: ComplaintStatus): ComplaintStatus[] {
  return VALID_TRANSITIONS[current];
}

/** Returns true if the transition from → to is permitted. */
export function isValidTransition(from: ComplaintStatus, to: ComplaintStatus): boolean {
  return VALID_TRANSITIONS[from].includes(to);
}

/** Human-readable rejection reason for a forbidden transition. */
export function transitionRejectionReason(
  from: ComplaintStatus,
  to: ComplaintStatus,
): string {
  if (from === to) return 'The complaint is already in this state.';
  if (from === 'resolved') return 'Resolved is a terminal state. No further changes are allowed.';
  if (to === 'pending') return 'Reverting to Pending is not allowed. Status can only move forward.';
  if (from === 'pending' && to === 'resolved')
    return 'Skipping stages is not allowed. A complaint must be marked In Progress before it can be Resolved.';
  return `Transition from "${STATUS_LABELS[from]}" to "${STATUS_LABELS[to]}" is not permitted.`;
}

// ─── Audit / history ──────────────────────────────────────────────────────────

/** One recorded status change, stored inside the complaint. */
export interface StatusHistoryEntry {
  from: ComplaintStatus;
  to: ComplaintStatus;
  changedAt: string;      // ISO date string
  changedBy: string;      // staff name or student id
  remarks?: string;       // optional note at time of transition
}

// ─── Feedback & Follow-up ────────────────────────────────────────────────────

export interface ComplaintFeedback {
  rating: number;         // 1 to 5 stars
  comment?: string;       // Optional written comments
  submittedAt: string;    // ISO date string
  updatedAt?: string;     // ISO date string if edited
}

// ─── Complaint ────────────────────────────────────────────────────────────────

export interface Complaint {
  id: string;               // e.g. CF-2026-0001
  title: string;
  description: string;
  building: string;
  floor: string;
  roomNumber: string;
  category: IssueCategory;
  status: ComplaintStatus;
  submittedAt: string;      // ISO date string
  resolvedAt?: string;      // ISO date string – set when status becomes resolved
  imageBase64?: string;     // Optional photo attached by student
  upvotes: string[];        // Array of studentId strings (one per student)
  assignedStaff?: string;
  resolutionRemarks?: string;
  submittedBy: string;      // studentId
  overdueThresholdDays: number; // days before flagged overdue (default 7)
  statusHistory: StatusHistoryEntry[]; // audit trail of every status transition

  // Post-resolution feedback & follow-up linkage
  feedback?: ComplaintFeedback;
  isFollowUp?: boolean;
  originalComplaintId?: string; // Reference to original parent complaint
  followUpReason?: string;      // Required explanation for why issue remains/recurred
}

export interface Student {
  id: string;
  name: string;
  rollNumber: string;
  branch: string;
  year: number;
}

export interface Staff {
  id: string;
  name: string;
  role: string;
  department: string;
  phone: string;
}

// Upvote action result
export type UpvoteResult = 'added' | 'removed' | 'duplicate';
