import type { Complaint, ComplaintStatus } from '../types';

/** Days between two ISO date strings (returns >= 0, or 0 if dates are invalid) */
export function daysBetween(from: string, to?: string): number {
  const start = new Date(from).getTime();
  const end = to ? new Date(to).getTime() : Date.now();
  if (isNaN(start) || isNaN(end)) return 0;
  return Math.max(0, Math.floor((end - start) / 86_400_000));
}

/** Returns true if the complaint is overdue */
export function isOverdue(complaint: Complaint): boolean {
  if (complaint.status === 'resolved') return false;
  return daysBetween(complaint.submittedAt) > complaint.overdueThresholdDays;
}

/** Resolution time in days (only for resolved complaints) */
export function resolutionDays(complaint: Complaint): number | null {
  if (complaint.status !== 'resolved' || !complaint.resolvedAt) return null;
  return daysBetween(complaint.submittedAt, complaint.resolvedAt);
}

/** Average resolution time (only over resolved complaints) */
export function avgResolutionDays(complaints: Complaint[]): number | null {
  const resolved = complaints.filter(c => c.status === 'resolved' && c.resolvedAt);
  if (!resolved.length) return null;
  const total = resolved.reduce((sum, c) => sum + (resolutionDays(c) ?? 0), 0);
  return Math.round((total / resolved.length) * 10) / 10;
}

/** Median resolution time */
export function medianResolutionDays(complaints: Complaint[]): number | null {
  const days = complaints
    .filter(c => c.status === 'resolved' && c.resolvedAt)
    .map(c => resolutionDays(c) ?? 0)
    .sort((a, b) => a - b);
  if (!days.length) return null;
  const mid = Math.floor(days.length / 2);
  return days.length % 2 !== 0 ? days[mid] : (days[mid - 1] + days[mid]) / 2;
}

/** Group complaints by building name */
export function groupByBuilding(
  complaints: Complaint[]
): Record<string, Complaint[]> {
  return complaints.reduce<Record<string, Complaint[]>>((acc, c) => {
    (acc[c.building] ??= []).push(c);
    return acc;
  }, {});
}

/** Group complaints by status */
export function groupByStatus(complaints: Complaint[]): Record<ComplaintStatus, Complaint[]> {
  return {
    pending: complaints.filter(c => c.status === 'pending'),
    'in-progress': complaints.filter(c => c.status === 'in-progress'),
    resolved: complaints.filter(c => c.status === 'resolved'),
  };
}

/** Format date string to readable format */
export function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString('en-IN', {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
  });
}

/** Format relative time (e.g. "3 days ago") */
export function relativeTime(iso: string): string {
  const days = daysBetween(iso);
  if (days === 0) return 'Today';
  if (days === 1) return 'Yesterday';
  if (days < 30) return `${days} days ago`;
  const months = Math.floor(days / 30);
  return `${months} month${months > 1 ? 's' : ''} ago`;
}
