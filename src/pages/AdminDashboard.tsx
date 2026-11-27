import { useState, useMemo } from 'react';
import { Link } from 'react-router-dom';
import {
  getComplaints,
  transitionComplaintStatus,
  updateComplaint,
  getFollowUpsForComplaint,
} from '../utils/storage';
import { MOCK_STAFF } from '../data/mockData';
import { StatusBadge } from '../components/shared/StatusBadge';
import { isOverdue, formatDate, daysBetween } from '../utils/helpers';
import type { Complaint, ComplaintStatus } from '../types';
import {
  getAllowedNextStatuses,
  STATUS_LABELS,
  transitionRejectionReason,
  isValidTransition,
} from '../types';
import {
  Search,
  ChevronDown,
  ChevronUp,
  ShieldCheck,
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Lock,
  Clock,
  History,
  RotateCcw,
  Star,
  ExternalLink,
} from 'lucide-react';

// ─── Status transition step indicator ────────────────────────────────────────
const ORDERED_STATUSES: ComplaintStatus[] = ['pending', 'in-progress', 'resolved'];

function StatusStepper({ current }: { current: ComplaintStatus }) {
  const currentIdx = ORDERED_STATUSES.indexOf(current);
  return (
    <div className="flex items-center gap-0">
      {ORDERED_STATUSES.map((s, i) => {
        const done = i < currentIdx;
        const active = i === currentIdx;
        return (
          <div key={s} className="flex items-center">
            <div
              className={`flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium transition-colors ${
                active
                  ? 'bg-blue-600 text-white'
                  : done
                    ? 'bg-emerald-100 text-emerald-700'
                    : 'bg-slate-100 text-slate-400'
              }`}
            >
              {done ? <CheckCircle2 size={11} /> : active ? <Clock size={11} /> : <Lock size={11} />}
              {STATUS_LABELS[s]}
            </div>
            {i < ORDERED_STATUSES.length - 1 && (
              <ArrowRight
                size={13}
                className={`mx-1 flex-shrink-0 ${i < currentIdx ? 'text-emerald-400' : 'text-slate-200'}`}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}

// ─── Status history timeline ──────────────────────────────────────────────────
function StatusTimeline({ complaint }: { complaint: Complaint }) {
  const history = complaint.statusHistory ?? [];

  return (
    <div className="space-y-2">
      {/* Submission — always the first entry */}
      <div className="flex gap-3">
        <div className="flex flex-col items-center">
          <div className="w-6 h-6 rounded-full bg-slate-200 flex items-center justify-center flex-shrink-0">
            <span className="text-slate-500 text-xs">📋</span>
          </div>
          {history.length > 0 && <div className="w-px flex-1 bg-slate-100 my-1" />}
        </div>
        <div className="pb-2">
          <p className="text-xs font-semibold text-slate-700">Complaint Submitted</p>
          <p className="text-xs text-slate-400 mt-0.5">{formatDate(complaint.submittedAt)}</p>
        </div>
      </div>

      {/* Recorded transitions */}
      {history.map((entry, idx) => {
        const isLast = idx === history.length - 1;
        const isResolved = entry.to === 'resolved';
        return (
          <div key={idx} className="flex gap-3">
            <div className="flex flex-col items-center">
              <div
                className={`w-6 h-6 rounded-full flex items-center justify-center flex-shrink-0 ${
                  isResolved ? 'bg-emerald-100' : 'bg-blue-100'
                }`}
              >
                {isResolved
                  ? <CheckCircle2 size={13} className="text-emerald-600" />
                  : <ArrowRight size={13} className="text-blue-600" />
                }
              </div>
              {!isLast && <div className="w-px flex-1 bg-slate-100 my-1" />}
            </div>
            <div className="pb-2">
              <p className="text-xs font-semibold text-slate-700">
                {STATUS_LABELS[entry.from]} → {STATUS_LABELS[entry.to]}
              </p>
              <p className="text-xs text-slate-400">
                {formatDate(entry.changedAt)} · by <span className="text-slate-600">{entry.changedBy}</span>
              </p>
              {entry.remarks && (
                <p className="text-xs text-slate-500 mt-0.5 italic">"{entry.remarks}"</p>
              )}
            </div>
          </div>
        );
      })}

      {/* Terminal state note */}
      {complaint.status === 'resolved' && (
        <div className="flex gap-3">
          <div className="w-6 h-6 rounded-full bg-emerald-50 border border-emerald-200 flex items-center justify-center flex-shrink-0">
            <Lock size={11} className="text-emerald-500" />
          </div>
          <p className="text-xs text-emerald-600 font-medium self-center">
            Terminal state — no further transitions allowed.
          </p>
        </div>
      )}
    </div>
  );
}

// ─── Individual complaint row ─────────────────────────────────────────────────
function AdminRow({
  complaint,
  onSave,
  onSelectComplaint,
}: {
  complaint: Complaint;
  onSave: () => void;
  onSelectComplaint?: (id: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);

  // Non-status fields (can be changed without triggering a transition)
  const [staff, setStaff] = useState(complaint.assignedStaff ?? '');
  const [remarks, setRemarks] = useState('');

  // Transition state
  const [selectedNext, setSelectedNext] = useState<ComplaintStatus | ''>('');
  const [transitionError, setTransitionError] = useState('');
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [showHistory, setShowHistory] = useState(false);

  const overdue = isOverdue(complaint);
  const age = daysBetween(complaint.submittedAt);
  const allowedNext = getAllowedNextStatuses(complaint.status);
  const isTerminal = allowedNext.length === 0;

  const childFollowUps = useMemo(() => {
    return getFollowUpsForComplaint(complaint.id);
  }, [complaint.id]);

  // Validate the selected transition live (without committing)
  function getSelectionError(next: ComplaintStatus | ''): string {
    if (!next) return '';
    if (!isValidTransition(complaint.status, next)) {
      return transitionRejectionReason(complaint.status, next);
    }
    return '';
  }

  function handleNextChange(val: ComplaintStatus | '') {
    setSelectedNext(val);
    setTransitionError(getSelectionError(val));
  }

  /** Save staff/remarks metadata without changing status */
  function handleSaveMetadata() {
    setSaving(true);
    const updated: Complaint = {
      ...complaint,
      assignedStaff: staff || undefined,
    };
    updateComplaint(updated);
    setSaving(false);
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
    onSave();
  }

  /** Apply the validated status transition */
  function handleTransition() {
    if (!selectedNext) return;

    const staffName = staff.trim() || 'Admin';
    const result = transitionComplaintStatus(
      complaint.id,
      selectedNext,
      staffName,
      remarks.trim() || undefined,
      staff || undefined,
    );

    if (!result.ok) {
      setTransitionError(result.reason);
      return;
    }

    setTransitionError('');
    setSelectedNext('');
    setRemarks('');
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
    onSave();
  }

  return (
    <div
      className={`card transition-shadow duration-150 ${
        overdue && complaint.status !== 'resolved' ? 'border-l-4 border-red-400' : ''
      }`}
    >
      {/* ── Summary row (always visible) ──────────────────────────────────── */}
      <button
        className="w-full flex items-start justify-between gap-3 text-left"
        onClick={() => setExpanded(e => !e)}
      >
        <div className="flex-1 min-w-0">
          <div className="flex flex-wrap items-center gap-2 mb-1.5">
            <span className="text-xs font-mono text-slate-400">{complaint.id}</span>
            <StatusBadge status={complaint.status} overdue={overdue} />
            {complaint.isFollowUp && (
              <span className="inline-flex items-center gap-1 text-[11px] font-medium text-amber-800 bg-amber-100 px-2 py-0.5 rounded-full">
                <RotateCcw size={10} /> Follow-up {complaint.originalComplaintId && `(to ${complaint.originalComplaintId})`}
              </span>
            )}
            {childFollowUps.length > 0 && (
              <span className="inline-flex items-center gap-1 text-[11px] font-medium text-blue-700 bg-blue-100 px-2 py-0.5 rounded-full">
                <RotateCcw size={10} /> {childFollowUps.length} Follow-up{childFollowUps.length > 1 ? 's' : ''}
              </span>
            )}
            {complaint.feedback && (
              <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-amber-700 bg-amber-50 px-2 py-0.5 rounded border border-amber-200">
                <Star size={10} className="fill-amber-400 text-amber-400" />
                {complaint.feedback.rating}/5
              </span>
            )}
            {overdue && (
              <span className="flex items-center gap-0.5 text-xs text-red-500 font-medium">
                <AlertTriangle size={11} />
                {age}d old
              </span>
            )}
            {isTerminal && (
              <span className="flex items-center gap-1 text-xs text-slate-400 bg-slate-100 px-2 py-0.5 rounded-full">
                <Lock size={10} />
                Terminal
              </span>
            )}
          </div>
          <p className="text-sm font-semibold text-slate-800">{complaint.title}</p>
          <p className="text-xs text-slate-500 mt-0.5">
            {complaint.building} · {complaint.floor} · {complaint.roomNumber} · {complaint.category}
          </p>
        </div>
        <div className="flex-shrink-0 text-slate-400">
          {expanded ? <ChevronUp size={18} /> : <ChevronDown size={18} />}
        </div>
      </button>

      {/* ── Expanded panel ─────────────────────────────────────────────────── */}
      {expanded && (
        <div className="mt-4 border-t border-slate-100 pt-4 space-y-5">

          {/* Description & metadata */}
          <div>
            <p className="text-sm text-slate-600 leading-relaxed">{complaint.description}</p>
            <p className="text-xs text-slate-400 mt-1">Submitted: {formatDate(complaint.submittedAt)}</p>
            {complaint.resolvedAt && (
              <p className="text-xs text-emerald-600 mt-0.5">
                Resolved: {formatDate(complaint.resolvedAt)}
                {' '}({daysBetween(complaint.submittedAt, complaint.resolvedAt)}d resolution time)
              </p>
            )}
          </div>

          {complaint.imageBase64 && (
            <img
              src={complaint.imageBase64}
              alt="Complaint photo"
              className="rounded-lg max-h-48 object-cover"
            />
          )}

          {/* Linked Parent Information if this is a follow-up */}
          {complaint.isFollowUp && complaint.originalComplaintId && (
            <div className="p-3 bg-amber-50 border border-amber-200 rounded-xl space-y-1.5 text-xs">
              <div className="flex items-center justify-between">
                <span className="font-semibold text-amber-900 flex items-center gap-1.5">
                  <RotateCcw size={13} className="text-amber-600" />
                  Follow-up Complaint
                </span>
                {onSelectComplaint && (
                  <button
                    type="button"
                    onClick={() => onSelectComplaint(complaint.originalComplaintId!)}
                    className="text-blue-700 hover:underline font-mono font-medium inline-flex items-center gap-1"
                  >
                    View Original ({complaint.originalComplaintId}) <ExternalLink size={10} />
                  </button>
                )}
              </div>
              {complaint.followUpReason && (
                <p className="text-amber-800">
                  <span className="font-semibold">Reason for follow-up:</span> {complaint.followUpReason}
                </p>
              )}
            </div>
          )}

          {/* Linked Child Follow-ups list */}
          {childFollowUps.length > 0 && (
            <div className="p-3 bg-blue-50 border border-blue-200 rounded-xl space-y-2 text-xs">
              <p className="font-semibold text-blue-900 flex items-center gap-1.5">
                <RotateCcw size={13} className="text-blue-600" />
                Linked Follow-up Complaints ({childFollowUps.length})
              </p>
              <div className="space-y-1.5">
                {childFollowUps.map(cf => (
                  <div key={cf.id} className="flex items-center justify-between bg-white p-2 rounded-lg border border-blue-100">
                    <div className="flex items-center gap-2">
                      <span className="font-mono font-medium text-slate-700">{cf.id}</span>
                      <StatusBadge status={cf.status} />
                      <span className="text-slate-600 truncate max-w-xs">{cf.title}</span>
                    </div>
                    {onSelectComplaint && (
                      <button
                        type="button"
                        onClick={() => onSelectComplaint(cf.id)}
                        className="text-blue-600 hover:underline font-medium inline-flex items-center gap-1"
                      >
                        Inspect Follow-up →
                      </button>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Student Resolution Feedback display in Admin */}
          {complaint.feedback && (
            <div className="p-3 bg-amber-50/70 border border-amber-200 rounded-xl text-xs space-y-1.5">
              <div className="flex items-center justify-between">
                <span className="font-semibold text-amber-900 flex items-center gap-1">
                  <Star size={13} className="fill-amber-400 text-amber-500" />
                  Student Resolution Rating & Feedback
                </span>
                <span className="font-bold text-amber-800">{complaint.feedback.rating} / 5 Stars</span>
              </div>
              {complaint.feedback.comment ? (
                <p className="text-slate-700 italic bg-white p-2 rounded border border-amber-100">
                  "{complaint.feedback.comment}"
                </p>
              ) : (
                <p className="text-slate-400 italic">No written comment provided.</p>
              )}
              <p className="text-[11px] text-slate-400">
                Submitted by student: {formatDate(complaint.feedback.submittedAt)}
                {complaint.feedback.updatedAt && ` · Updated: ${formatDate(complaint.feedback.updatedAt)}`}
              </p>
            </div>
          )}

          {/* Status stepper */}
          <div>
            <p className="text-xs font-medium text-slate-500 mb-2">Status Workflow</p>
            <StatusStepper current={complaint.status} />
          </div>

          {/* ── Staff assignment (non-status metadata) ───────────────────── */}
          <div className="grid sm:grid-cols-2 gap-4 p-4 bg-slate-50 rounded-xl">
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">
                Assign Responsible Staff
              </label>
              <select
                className="select text-sm"
                value={staff}
                onChange={e => setStaff(e.target.value)}
                disabled={isTerminal}
              >
                <option value="">Unassigned</option>
                {MOCK_STAFF.map(s => (
                  <option key={s.id} value={s.name}>
                    {s.name} ({s.role})
                  </option>
                ))}
              </select>
            </div>
            <div className="flex items-end">
              <button
                onClick={handleSaveMetadata}
                disabled={saving || isTerminal}
                className="btn-secondary text-sm w-full"
              >
                {saved ? '✓ Staff Saved' : 'Save Staff'}
              </button>
            </div>
          </div>

          {/* ── Status transition panel ───────────────────────────────────── */}
          {isTerminal ? (
            <div className="flex items-center gap-2 bg-emerald-50 border border-emerald-200 rounded-xl px-4 py-3">
              <Lock size={15} className="text-emerald-500 flex-shrink-0" />
              <div>
                <p className="text-sm font-semibold text-emerald-700">Resolved — Terminal State</p>
                <p className="text-xs text-emerald-600 mt-0.5">
                  This complaint is closed. No further status transitions are permitted.
                </p>
              </div>
            </div>
          ) : (
            <div className="p-4 bg-blue-50 border border-blue-100 rounded-xl space-y-3">
              <p className="text-xs font-semibold text-blue-800 uppercase tracking-wide">
                Advance Status
              </p>

              {/* Allowed next statuses */}
              <div className="flex flex-wrap gap-2">
                {allowedNext.map(next => (
                  <button
                    key={next}
                    onClick={() => handleNextChange(selectedNext === next ? '' : next)}
                    className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg border text-sm font-medium transition-all duration-100 ${
                      selectedNext === next
                        ? 'bg-blue-600 border-blue-600 text-white shadow-sm'
                        : 'bg-white border-slate-200 text-slate-700 hover:border-blue-300 hover:text-blue-700'
                    }`}
                  >
                    <ArrowRight size={13} />
                    Move to {STATUS_LABELS[next]}
                  </button>
                ))}
              </div>

              {/* Transition error message */}
              {transitionError && (
                <div className="flex items-start gap-2 bg-red-50 border border-red-200 rounded-lg px-3 py-2">
                  <AlertTriangle size={14} className="text-red-500 flex-shrink-0 mt-0.5" />
                  <p className="text-xs text-red-700">{transitionError}</p>
                </div>
              )}

              {/* Remarks for this transition */}
              {selectedNext && !transitionError && (
                <div className="space-y-2 animate-in fade-in">
                  <label className="block text-xs font-medium text-slate-600">
                    Transition remarks{' '}
                    {selectedNext === 'resolved' && (
                      <span className="text-red-500">* (required for resolution)</span>
                    )}
                  </label>
                  <textarea
                    className="input resize-none text-sm"
                    rows={2}
                    placeholder={
                      selectedNext === 'resolved'
                        ? 'Describe what was done to resolve this issue…'
                        : 'Add a note (optional) — e.g. staff assigned, work started…'
                    }
                    value={remarks}
                    onChange={e => setRemarks(e.target.value)}
                  />
                  <button
                    onClick={handleTransition}
                    disabled={selectedNext === 'resolved' && !remarks.trim()}
                    className="btn-primary text-sm w-full"
                  >
                    {saving
                      ? 'Saving…'
                      : `Confirm: Move to ${STATUS_LABELS[selectedNext]}`}
                  </button>
                  {selectedNext === 'resolved' && !remarks.trim() && (
                    <p className="text-xs text-amber-600">
                      Resolution remarks are required before closing a complaint.
                    </p>
                  )}
                </div>
              )}

              {/* Explain why other statuses are blocked */}
              <div className="border-t border-blue-100 pt-2 space-y-1">
                {(['pending', 'in-progress', 'resolved'] as ComplaintStatus[])
                  .filter(s => !allowedNext.includes(s) && s !== complaint.status)
                  .map(s => (
                    <p key={s} className="text-xs text-slate-400 flex items-center gap-1.5">
                      <Lock size={10} className="text-slate-300" />
                      <span className="font-medium text-slate-500">{STATUS_LABELS[s]}</span>
                      {' — '}
                      {transitionRejectionReason(complaint.status, s)}
                    </p>
                  ))}
              </div>
            </div>
          )}

          {/* ── Status history timeline & Link to Student View ─────────────── */}
          <div className="flex items-center justify-between border-t border-slate-100 pt-3">
            <button
              className="flex items-center gap-1.5 text-xs font-medium text-slate-500 hover:text-slate-700"
              onClick={() => setShowHistory(h => !h)}
            >
              <History size={13} />
              {showHistory ? 'Hide' : 'Show'} Status History
              ({(complaint.statusHistory ?? []).length} transition{(complaint.statusHistory ?? []).length !== 1 ? 's' : ''})
            </button>

            <Link
              to={`/complaints/${complaint.id}`}
              className="text-xs text-blue-600 hover:underline inline-flex items-center gap-1"
            >
              Open Student Detail View <ExternalLink size={11} />
            </Link>
          </div>

          {showHistory && (
            <div className="bg-slate-50 rounded-xl p-4">
              <StatusTimeline complaint={complaint} />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ─── Main admin dashboard ─────────────────────────────────────────────────────
export default function AdminDashboard() {
  const [tick, setTick] = useState(0);
  const [search, setSearch] = useState('');
  const [filterStatus, setFilterStatus] = useState('');
  const [filterType, setFilterType] = useState<'' | 'original' | 'followup'>('');

  const complaints = useMemo(() => getComplaints(), [tick]);

  const filtered = useMemo(() => {
    let r = complaints;
    if (filterStatus) r = r.filter((c: Complaint) => c.status === filterStatus);
    if (filterType === 'original') r = r.filter((c: Complaint) => !c.isFollowUp);
    if (filterType === 'followup') r = r.filter((c: Complaint) => c.isFollowUp);
    if (search.trim()) {
      const q = search.toLowerCase();
      r = r.filter((c: Complaint) =>
        c.title.toLowerCase().includes(q) ||
        c.id.toLowerCase().includes(q) ||
        c.building.toLowerCase().includes(q) ||
        (c.originalComplaintId && c.originalComplaintId.toLowerCase().includes(q))
      );
    }
    // Overdue first, then newest
    return [...r].sort((a: Complaint, b: Complaint) => {
      const aOver = isOverdue(a) ? 1 : 0;
      const bOver = isOverdue(b) ? 1 : 0;
      if (bOver !== aOver) return bOver - aOver;
      return new Date(b.submittedAt).getTime() - new Date(a.submittedAt).getTime();
    });
  }, [complaints, filterStatus, filterType, search]);

  const overdueCount = complaints.filter(isOverdue).length;
  const pendingCount = complaints.filter((c: Complaint) => c.status === 'pending').length;
  const inProgressCount = complaints.filter((c: Complaint) => c.status === 'in-progress').length;
  const followUpCount = complaints.filter((c: Complaint) => c.isFollowUp).length;

  return (
    <div className="max-w-4xl mx-auto space-y-5">
      {/* Header */}
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2">
          <ShieldCheck size={22} className="text-blue-600" />
          <div>
            <h2 className="text-xl font-bold text-slate-800">Admin Panel</h2>
            <p className="text-sm text-slate-500">
              {complaints.length} complaints · {pendingCount} pending · {inProgressCount} in-progress · {overdueCount} overdue · {followUpCount} follow-up{followUpCount !== 1 ? 's' : ''}
            </p>
          </div>
        </div>
      </div>

      {/* Workflow rule reminder */}
      <div className="flex items-start gap-3 bg-blue-50 border border-blue-100 rounded-xl px-4 py-3">
        <ArrowRight size={15} className="text-blue-500 flex-shrink-0 mt-0.5" />
        <p className="text-xs text-blue-700">
          <span className="font-semibold">Workflow rules:</span> Status transitions strictly forward (<span className="font-medium">Pending → In Progress → Resolved</span>). Resolved is terminal. Follow-up tickets run their own independent lifecycle without altering original resolved complaints.
        </p>
      </div>

      {/* Overdue alert */}
      {overdueCount > 0 && (
        <div className="flex items-center gap-3 bg-red-50 border border-red-200 rounded-xl px-4 py-3">
          <AlertTriangle size={16} className="text-red-500" />
          <p className="text-sm text-red-700 font-medium">
            {overdueCount} complaint{overdueCount !== 1 ? 's are' : ' is'} overdue and need immediate attention.
          </p>
        </div>
      )}

      {/* Search + filter */}
      <div className="card flex flex-col sm:flex-row gap-3">
        <div className="relative flex-1">
          <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
          <input
            className="input pl-9 text-sm"
            placeholder="Search by title, ID, building, parent ID…"
            value={search}
            onChange={e => setSearch(e.target.value)}
          />
        </div>
        <select
          className="select text-sm w-auto"
          value={filterStatus}
          onChange={e => setFilterStatus(e.target.value)}
        >
          <option value="">All Statuses</option>
          <option value="pending">Pending</option>
          <option value="in-progress">In Progress</option>
          <option value="resolved">Resolved</option>
        </select>
        <select
          className="select text-sm w-auto"
          value={filterType}
          onChange={e => setFilterType(e.target.value as '' | 'original' | 'followup')}
        >
          <option value="">All Complaint Types</option>
          <option value="original">Original Only</option>
          <option value="followup">Follow-ups Only</option>
        </select>
        {(search || filterStatus || filterType) && (
          <button
            type="button"
            onClick={() => { setSearch(''); setFilterStatus(''); setFilterType(''); }}
            className="btn-secondary text-xs px-3"
          >
            Clear
          </button>
        )}
      </div>

      {/* Complaint rows */}
      <div className="space-y-3">
        {filtered.map((c: Complaint) => (
          <AdminRow
            key={c.id}
            complaint={c}
            onSave={() => setTick(t => t + 1)}
            onSelectComplaint={(id: string) => {
              setSearch(id);
              setFilterStatus('');
              setFilterType('');
            }}
          />
        ))}
        {filtered.length === 0 && (
          <div className="text-center py-16 text-slate-400">
            <p className="text-3xl mb-2">🔍</p>
            <p>No complaints match your filters.</p>
          </div>
        )}
      </div>
    </div>
  );
}
