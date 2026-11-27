import { describe, it, expect, beforeEach } from 'vitest';
import {
  createFollowUpComplaint,
  addComplaint,
  getComplaints,
  saveComplaints,
} from '../utils/storage';
import type { Complaint } from '../types';

describe('createFollowUpComplaint (M-01)', () => {
  const resolvedParent: Complaint = {
    id: 'CF-2026-9001',
    title: 'Water leak in Restroom 101',
    description: 'Pipe leaking under the sink.',
    building: 'Block A',
    floor: '1st Floor',
    roomNumber: '101',
    category: 'Plumbing',
    status: 'resolved',
    submittedAt: '2026-01-01T10:00:00.000Z',
    resolvedAt: '2026-01-03T10:00:00.000Z',
    upvotes: [],
    submittedBy: 'cognito-user-abc',
    overdueThresholdDays: 7,
    statusHistory: [],
  };

  beforeEach(() => {
    localStorage.clear();
    saveComplaints([resolvedParent]);
  });

  it('rejects follow-up creation if submittedBy identity is missing or empty', () => {
    const result1 = createFollowUpComplaint({
      originalComplaintId: 'CF-2026-9001',
      followUpReason: 'The pipe started leaking again under water pressure.',
      submittedBy: '',
    });
    expect(result1.ok).toBe(false);
    expect(result1.reason).toContain('Author identity (submittedBy) is required');

    const result2 = createFollowUpComplaint({
      originalComplaintId: 'CF-2026-9001',
      followUpReason: 'The pipe started leaking again under water pressure.',
      submittedBy: '   ',
    });
    expect(result2.ok).toBe(false);
    expect(result2.reason).toContain('Author identity (submittedBy) is required');
  });

  it('rejects follow-up creation if reason is less than 10 characters', () => {
    const result = createFollowUpComplaint({
      originalComplaintId: 'CF-2026-9001',
      followUpReason: 'leak',
      submittedBy: 'cognito-user-abc',
    });
    expect(result.ok).toBe(false);
    expect(result.reason).toContain('at least 10 characters');
  });

  it('rejects follow-up creation if original complaint does not exist or is not resolved', () => {
    const notFound = createFollowUpComplaint({
      originalComplaintId: 'CF-2026-9999',
      followUpReason: 'The pipe started leaking again under water pressure.',
      submittedBy: 'cognito-user-abc',
    });
    expect(notFound.ok).toBe(false);
    expect(notFound.reason).toContain('was not found');

    const pendingParent: Complaint = {
      ...resolvedParent,
      id: 'CF-2026-9002',
      status: 'pending',
    };
    addComplaint(pendingParent);

    const notResolved = createFollowUpComplaint({
      originalComplaintId: 'CF-2026-9002',
      followUpReason: 'The pipe started leaking again under water pressure.',
      submittedBy: 'cognito-user-abc',
    });
    expect(notResolved.ok).toBe(false);
    expect(notResolved.reason).toContain('can only be created against Resolved complaints');
  });

  it('rejects duplicate follow-ups if an active follow-up already exists', () => {
    const firstFollowUp = createFollowUpComplaint({
      originalComplaintId: 'CF-2026-9001',
      followUpReason: 'The pipe started leaking again under water pressure.',
      submittedBy: 'cognito-user-abc',
    });
    expect(firstFollowUp.ok).toBe(true);

    const duplicate = createFollowUpComplaint({
      originalComplaintId: 'CF-2026-9001',
      followUpReason: 'Another follow-up on the same complaint before resolving first.',
      submittedBy: 'cognito-user-abc',
    });
    expect(duplicate.ok).toBe(false);
    expect(duplicate.reason).toContain('already exists for this complaint');
  });

  it('creates follow-up complaint with exact submittedBy identity and fresh lifecycle without mock fallbacks', () => {
    const result = createFollowUpComplaint({
      originalComplaintId: 'CF-2026-9001',
      followUpReason: 'The pipe started leaking again under water pressure.',
      submittedBy: 'cognito-user-xyz',
    });

    expect(result.ok).toBe(true);
    expect(result.complaint).toBeDefined();
    expect(result.complaint?.submittedBy).toBe('cognito-user-xyz');
    expect(result.complaint?.status).toBe('pending');
    expect(result.complaint?.isFollowUp).toBe(true);
    expect(result.complaint?.originalComplaintId).toBe('CF-2026-9001');
    expect(result.complaint?.upvotes).toEqual([]);
    expect(result.complaint?.feedback).toBeUndefined();

    // Verify persisted in localStorage
    const all = getComplaints();
    const persisted = all.find((c) => c.id === result.complaint?.id);
    expect(persisted).toBeDefined();
    expect(persisted?.submittedBy).toBe('cognito-user-xyz');
  });

  it('confirms fresh demo seed resolved complaint is owned by default mock student mock-stu-001 (L-02)', () => {
    localStorage.clear();
    const all = getComplaints();
    const seedResolved = all.find((c) => c.id === 'CF-2024-0003');
    expect(seedResolved).toBeDefined();
    expect(seedResolved?.status).toBe('resolved');
    expect(seedResolved?.submittedBy).toBe('mock-stu-001');

    // Demonstrates default mock student can submit a follow-up on this resolved complaint
    const followUp = createFollowUpComplaint({
      originalComplaintId: 'CF-2024-0003',
      followUpReason: 'The projector stopped turning on again this afternoon.',
      submittedBy: 'mock-stu-001',
    });
    expect(followUp.ok).toBe(true);
    expect(followUp.complaint?.submittedBy).toBe('mock-stu-001');
  });
});
