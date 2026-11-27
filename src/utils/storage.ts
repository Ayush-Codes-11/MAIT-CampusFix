/**
 * Local-storage persistence layer for MAIT CampusFix.
 * All keys are namespaced under "campusfix_" to avoid collisions.
 *
 * NOTE: This is a prototype-grade storage layer.
 *       Replace with API calls when the backend is ready.
 */

import type { Complaint, ComplaintStatus, StatusHistoryEntry } from '../types';
import { isValidTransition, transitionRejectionReason } from '../types';
import { SEED_COMPLAINTS, MOCK_CURRENT_STUDENT } from '../data/mockData';

const KEYS = {
  COMPLAINTS: 'campusfix_complaints',
} as const;

// ─── Helpers ──────────────────────────────────────────────────────────────────
function readJSON<T>(key: string): T | null {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : null;
  } catch {
    return null;
  }
}

function writeJSON<T>(key: string, value: T): void {
  localStorage.setItem(key, JSON.stringify(value));
}

/**
 * Back-compat migration: older complaint records (from before this feature)
 * won't have a statusHistory array. Add an empty one so the type is satisfied.
 */
function migrateComplaint(c: Complaint): Complaint {
  return {
    ...c,
    upvotes: Array.isArray(c.upvotes) ? c.upvotes : [],
    statusHistory: Array.isArray(c.statusHistory) ? c.statusHistory : [],
    isFollowUp: c.isFollowUp ?? Boolean(c.originalComplaintId),
    overdueThresholdDays: typeof c.overdueThresholdDays === 'number' ? c.overdueThresholdDays : 7,
  };
}

// ─── Seed on first load ───────────────────────────────────────────────────────
export function ensureSeeded(): void {
  if (!localStorage.getItem(KEYS.COMPLAINTS)) {
    writeJSON(KEYS.COMPLAINTS, SEED_COMPLAINTS);
  }
}

// ─── Complaints CRUD ──────────────────────────────────────────────────────────
export function getComplaints(): Complaint[] {
  ensureSeeded();
  const raw = readJSON<Complaint[]>(KEYS.COMPLAINTS) ?? [];
  return raw.map(migrateComplaint);
}

export function getComplaintById(id: string): Complaint | undefined {
  return getComplaints().find(c => c.id === id);
}

export function saveComplaints(complaints: Complaint[]): void {
  writeJSON(KEYS.COMPLAINTS, complaints);
}

export function addComplaint(complaint: Complaint): void {
  const all = getComplaints();
  all.unshift(complaint);
  saveComplaints(all);
}

/**
 * Low-level update — bypasses transition validation.
 * Use only for non-status fields (staff assignment, remarks, etc.).
 * Status and resolvedAt are protected from accidental alteration.
 * For status changes, always use transitionComplaintStatus().
 */
export function updateComplaint(updated: Complaint): void {
  const all = getComplaints().map(c => {
    if (c.id === updated.id) {
      return {
        ...updated,
        status: c.status,
        resolvedAt: c.resolvedAt,
      };
    }
    return c;
  });
  saveComplaints(all);
}

// ─── Status transition (enforced) ─────────────────────────────────────────────
export type TransitionResult =
  | { ok: true; complaint: Complaint }
  | { ok: false; reason: string };

/**
 * The ONLY function that may change a complaint's status.
 *
 * Enforces strict forward-only transitions:
 *   pending → in-progress → resolved  (resolved is terminal)
 *
 * Records a StatusHistoryEntry on every successful transition.
 * Returns { ok: false, reason } on any invalid attempt — never throws.
 */
export function transitionComplaintStatus(
  complaintId: string,
  toStatus: ComplaintStatus,
  changedBy: string,
  remarks?: string,
  assignedStaff?: string,
): TransitionResult {
  const all = getComplaints();
  const idx = all.findIndex(c => c.id === complaintId);

  if (idx === -1) {
    return { ok: false, reason: `Complaint ${complaintId} not found.` };
  }

  const complaint = all[idx];
  const fromStatus = complaint.status;

  // ── Core enforcement ──────────────────────────────────────────────────────
  if (!isValidTransition(fromStatus, toStatus)) {
    return {
      ok: false,
      reason: transitionRejectionReason(fromStatus, toStatus),
    };
  }

  // ── Build history entry ───────────────────────────────────────────────────
  const historyEntry: StatusHistoryEntry = {
    from: fromStatus,
    to: toStatus,
    changedAt: new Date().toISOString(),
    changedBy,
    remarks: remarks?.trim() || undefined,
  };

  // ── Apply update ──────────────────────────────────────────────────────────
  const updated: Complaint = {
    ...complaint,
    status: toStatus,
    assignedStaff: assignedStaff !== undefined ? (assignedStaff || undefined) : complaint.assignedStaff,
    resolvedAt: toStatus === 'resolved' ? new Date().toISOString() : complaint.resolvedAt,
    resolutionRemarks: toStatus === 'resolved' && remarks ? remarks.trim() : complaint.resolutionRemarks,
    statusHistory: [...(complaint.statusHistory ?? []), historyEntry],
  };

  all[idx] = updated;
  saveComplaints(all);

  return { ok: true, complaint: updated };
}

// ─── Upvote logic ─────────────────────────────────────────────────────────────
// Rule: one upvote per student per complaint; allow removing the upvote.
export function toggleUpvote(complaintId: string): 'added' | 'removed' {
  const studentId = MOCK_CURRENT_STUDENT.id;
  const all = getComplaints();
  const complaint = all.find(c => c.id === complaintId);
  if (!complaint) throw new Error(`Complaint ${complaintId} not found`);

  const alreadyVoted = complaint.upvotes.includes(studentId);
  if (alreadyVoted) {
    complaint.upvotes = complaint.upvotes.filter(uid => uid !== studentId);
  } else {
    complaint.upvotes.push(studentId);
  }

  saveComplaints(all);
  return alreadyVoted ? 'removed' : 'added';
}

export function hasUpvoted(complaintId: string): boolean {
  const studentId = MOCK_CURRENT_STUDENT.id;
  const complaint = getComplaintById(complaintId);
  return complaint?.upvotes.includes(studentId) ?? false;
}

// ─── Post-Resolution Feedback ────────────────────────────────────────────────

export interface SaveFeedbackResult {
  ok: boolean;
  reason?: string;
  complaint?: Complaint;
}

export function saveComplaintFeedback(
  complaintId: string,
  rating: number,
  comment?: string,
): SaveFeedbackResult {
  if (rating < 1 || rating > 5) {
    return { ok: false, reason: 'Rating must be between 1 and 5 stars.' };
  }

  const all = getComplaints();
  const idx = all.findIndex(c => c.id === complaintId);
  if (idx === -1) {
    return { ok: false, reason: `Complaint ${complaintId} not found.` };
  }

  const complaint = all[idx];
  if (complaint.status !== 'resolved') {
    return { ok: false, reason: 'Feedback can only be submitted for resolved complaints.' };
  }

  const existing = complaint.feedback;
  const now = new Date().toISOString();

  complaint.feedback = {
    rating,
    comment: comment?.trim() || undefined,
    submittedAt: existing ? existing.submittedAt : now,
    ...(existing ? { updatedAt: now } : {}),
  };

  all[idx] = complaint;
  saveComplaints(all);

  return { ok: true, complaint };
}

// ─── Follow-up Complaints ─────────────────────────────────────────────────────

/** Returns all follow-up complaints directly linked to this original complaint */
export function getFollowUpsForComplaint(originalComplaintId: string): Complaint[] {
  return getComplaints().filter(c => c.originalComplaintId === originalComplaintId);
}

/** Returns true if there is an open (pending or in-progress) follow-up */
export function hasActiveFollowUp(originalComplaintId: string): boolean {
  return getFollowUpsForComplaint(originalComplaintId).some(c => c.status !== 'resolved');
}

export interface CreateFollowUpParams {
  originalComplaintId: string;
  followUpReason: string;
  submittedBy: string;
  title?: string;
  description?: string;
  imageBase64?: string;
}

export interface CreateFollowUpResult {
  ok: boolean;
  reason?: string;
  complaint?: Complaint;
}

/**
 * Creates an independent follow-up complaint linked to a resolved parent.
 *
 * Rules:
 * - Parent must exist and be in 'resolved' status.
 * - Prevents duplicates: no new follow-up if an active (pending/in-progress) follow-up already exists.
 * - Generates unique ID with current year.
 * - Starts fresh Pending -> In Progress -> Resolved lifecycle.
 * - Original complaint status remains permanently Resolved.
 * - Requires a non-empty submittedBy identity (fails closed without fallback to mock IDs).
 */
export function createFollowUpComplaint(
  params: CreateFollowUpParams,
): CreateFollowUpResult {
  const { originalComplaintId, followUpReason, submittedBy, title, description, imageBase64 } = params;

  if (!submittedBy || !submittedBy.trim()) {
    return {
      ok: false,
      reason: 'Author identity (submittedBy) is required to create a follow-up complaint.',
    };
  }

  if (!followUpReason || followUpReason.trim().length < 10) {
    return {
      ok: false,
      reason: 'Please provide a clear follow-up explanation (at least 10 characters) explaining why the issue is unresolved or recurred.',
    };
  }

  const parent = getComplaintById(originalComplaintId);
  if (!parent) {
    return { ok: false, reason: `Original complaint ${originalComplaintId} was not found.` };
  }

  if (parent.status !== 'resolved') {
    return {
      ok: false,
      reason: 'Follow-up complaints can only be created against Resolved complaints.',
    };
  }

  // Duplicate prevention safeguard:
  const activeFollowUp = getFollowUpsForComplaint(parent.id).find(c => c.status !== 'resolved');
  if (activeFollowUp) {
    return {
      ok: false,
      reason: `An active follow-up (${activeFollowUp.id} - ${activeFollowUp.status}) already exists for this complaint. Please wait for it to be resolved.`,
    };
  }

  const newId = generateComplaintId();
  const followUpComplaint: Complaint = {
    id: newId,
    title: title?.trim() || `[Follow-up] ${parent.title}`,
    description: description?.trim() || parent.description,
    building: parent.building,
    floor: parent.floor,
    roomNumber: parent.roomNumber,
    category: parent.category,
    status: 'pending',
    submittedAt: new Date().toISOString(),
    upvotes: [],
    imageBase64,
    submittedBy: submittedBy.trim(),
    overdueThresholdDays: parent.overdueThresholdDays ?? 7,
    statusHistory: [],
    isFollowUp: true,
    originalComplaintId: parent.id,
    followUpReason: followUpReason.trim(),
  };

  addComplaint(followUpComplaint);

  return { ok: true, complaint: followUpComplaint };
}

// ─── ID generator ─────────────────────────────────────────────────────────────
export function generateComplaintId(): string {
  const all = getComplaints();
  const year = new Date().getFullYear();
  const yearPrefix = `CF-${year}-`;
  const thisYearNumbers = all
    .filter(c => c.id.startsWith(yearPrefix))
    .map(c => parseInt(c.id.slice(yearPrefix.length), 10))
    .filter(n => !isNaN(n));

  let nextNum = thisYearNumbers.length > 0 ? Math.max(...thisYearNumbers) + 1 : 1;
  let id = `CF-${year}-${String(nextNum).padStart(4, '0')}`;
  while (all.some(c => c.id === id)) {
    nextNum++;
    id = `CF-${year}-${String(nextNum).padStart(4, '0')}`;
  }
  return id;
}

// ─── Reset (dev utility) ──────────────────────────────────────────────────────
export function resetToSeedData(): void {
  writeJSON(KEYS.COMPLAINTS, SEED_COMPLAINTS);
}
