import { useState, useMemo, useRef } from 'react';
import { useParams, Link, useNavigate } from 'react-router-dom';
import {
  getComplaintById,
  getComplaints,
  saveComplaintFeedback,
  createFollowUpComplaint,
  getFollowUpsForComplaint,
} from '../utils/storage';
import { useAuth } from '../context/AuthContext';
import { StatusBadge } from '../components/shared/StatusBadge';
import { UpvoteButton } from '../components/shared/UpvoteButton';
import {
  isOverdue,
  formatDate,
  relativeTime,
  daysBetween,
} from '../utils/helpers';
import { STATUS_LABELS } from '../types';
import {
  ArrowLeft,
  MapPin,
  Calendar,
  Tag,
  Star,
  CheckCircle2,
  Clock,
  ArrowRight,
  AlertTriangle,
  RotateCcw,
  Upload,
  X,
  ExternalLink,
  ShieldAlert,
} from 'lucide-react';

export default function ComplaintDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { user, role } = useAuth();
  const [tick, setTick] = useState(0);

  // Form states for Feedback
  const [rating, setRating] = useState<number>(0);
  const [hoverRating, setHoverRating] = useState<number>(0);
  const [comment, setComment] = useState('');
  const [isEditingFeedback, setIsEditingFeedback] = useState(false);
  const [feedbackSuccessMsg, setFeedbackSuccessMsg] = useState('');
  const [feedbackError, setFeedbackError] = useState('');

  // Form states for Follow-up
  const [showFollowUpForm, setShowFollowUpForm] = useState(false);
  const [followUpReason, setFollowUpReason] = useState('');
  const [followUpTitle, setFollowUpTitle] = useState('');
  const [followUpImage, setFollowUpImage] = useState<string | undefined>();
  const [followUpImagePreview, setFollowUpImagePreview] = useState<string | undefined>();
  const [followUpError, setFollowUpError] = useState('');
  const fileRef = useRef<HTMLInputElement>(null);

  const complaint = useMemo(() => {
    if (!id) return undefined;
    return getComplaintById(id);
  }, [id, tick]);

  const allComplaints = useMemo(() => getComplaints(), [tick]);

  const followUps = useMemo(() => {
    if (!complaint) return [];
    return getFollowUpsForComplaint(complaint.id);
  }, [complaint, tick]);

  const activeFollowUp = useMemo(() => {
    return followUps.find(c => c.status !== 'resolved');
  }, [followUps]);

  const parentComplaint = useMemo(() => {
    if (!complaint?.originalComplaintId) return undefined;
    return allComplaints.find(c => c.id === complaint.originalComplaintId);
  }, [complaint, allComplaints]);

  // Ownership and role permission checks
  const isOwner = Boolean(user && complaint?.submittedBy === user.userId);
  const isResolved = complaint?.status === 'resolved';
  const canSubmitFeedback = Boolean(role === 'student' && isOwner && isResolved);
  const canRaiseFollowUp = Boolean(role === 'student' && isOwner && isResolved);

  if (!complaint) {
    return (
      <div className="max-w-3xl mx-auto py-12 text-center space-y-4">
        <div className="w-16 h-16 rounded-full bg-slate-100 flex items-center justify-center mx-auto text-slate-400 text-2xl">
          🔍
        </div>
        <h2 className="text-xl font-bold text-slate-800">Complaint Not Found</h2>
        <p className="text-sm text-slate-500">
          The complaint with ID <span className="font-mono text-slate-700">{id}</span> does not exist or has been removed.
        </p>
        <Link to="/complaints" className="btn-primary inline-flex items-center gap-1.5 text-sm">
          <ArrowLeft size={14} /> Back to Complaints
        </Link>
      </div>
    );
  }

  const overdue = isOverdue(complaint);
  const resolutionTimeDays =
    complaint.resolvedAt
      ? daysBetween(complaint.submittedAt, complaint.resolvedAt)
      : null;

  function handleSaveFeedback(e: React.FormEvent) {
    e.preventDefault();
    if (!complaint) return;

    if (!canSubmitFeedback) {
      setFeedbackError('Only the original student author can submit or edit resolution feedback.');
      return;
    }

    if (rating < 1 || rating > 5) {
      setFeedbackError('Please select a rating between 1 and 5 stars.');
      return;
    }

    const res = saveComplaintFeedback(complaint.id, rating, comment);
    if (!res.ok) {
      setFeedbackError(res.reason || 'Failed to save feedback.');
      return;
    }

    setFeedbackError('');
    setIsEditingFeedback(false);
    setFeedbackSuccessMsg('Thank you! Your feedback has been recorded.');
    setTimeout(() => setFeedbackSuccessMsg(''), 3000);
    setTick(t => t + 1);
  }

  function handleStartEditFeedback() {
    if (complaint?.feedback) {
      setRating(complaint.feedback.rating);
      setComment(complaint.feedback.comment || '');
    }
    setIsEditingFeedback(true);
  }

  function handleImageUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    if (file.size > 5 * 1024 * 1024) {
      alert('Photo must be under 5 MB');
      return;
    }
    const reader = new FileReader();
    reader.onload = ev => {
      const res = ev.target?.result as string;
      setFollowUpImage(res);
      setFollowUpImagePreview(res);
    };
    reader.readAsDataURL(file);
  }

  function handleOpenFollowUpForm() {
    setFollowUpTitle(`[Follow-up] ${complaint?.title ?? ''}`);
    setFollowUpReason('');
    setFollowUpImage(undefined);
    setFollowUpImagePreview(undefined);
    setFollowUpError('');
    setShowFollowUpForm(true);
  }

  function handleSubmitFollowUp(e: React.FormEvent) {
    e.preventDefault();
    if (!complaint) return;

    if (!canRaiseFollowUp || !user?.userId) {
      setFollowUpError('Only the original student author can submit a follow-up ticket.');
      return;
    }

    if (!followUpReason.trim() || followUpReason.trim().length < 10) {
      setFollowUpError('Please describe why the issue remains unresolved or has returned (at least 10 characters).');
      return;
    }

    const res = createFollowUpComplaint({
      originalComplaintId: complaint.id,
      followUpReason: followUpReason.trim(),
      title: followUpTitle.trim() || `[Follow-up] ${complaint.title}`,
      description: complaint.description,
      imageBase64: followUpImage,
      submittedBy: user.userId,
    });

    if (!res.ok || !res.complaint) {
      setFollowUpError(res.reason || 'Failed to create follow-up complaint.');
      return;
    }

    setShowFollowUpForm(false);
    // Navigate to the newly created follow-up complaint
    navigate(`/complaints/${res.complaint.id}`);
  }

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      {/* Top back navigation */}
      <div className="flex items-center justify-between">
        <Link
          to="/complaints"
          className="inline-flex items-center gap-1.5 text-sm font-medium text-slate-600 hover:text-blue-600 transition-colors"
        >
          <ArrowLeft size={16} /> Back to Complaints
        </Link>
        <span className="text-xs text-slate-400 font-mono">ID: {complaint.id}</span>
      </div>

      {/* Linked Parent Notice if this is a follow-up */}
      {complaint.isFollowUp && complaint.originalComplaintId && (
        <div className="bg-amber-50 border border-amber-200 rounded-xl p-4 flex items-start gap-3">
          <RotateCcw size={18} className="text-amber-600 flex-shrink-0 mt-0.5" />
          <div className="flex-1 min-w-0 text-sm">
            <p className="font-semibold text-amber-900 flex items-center gap-2">
              Follow-up Complaint
              <Link
                to={`/complaints/${complaint.originalComplaintId}`}
                className="inline-flex items-center gap-1 font-mono text-xs text-blue-700 bg-blue-100 hover:bg-blue-200 px-2 py-0.5 rounded transition-colors"
              >
                Original: {complaint.originalComplaintId}
                <ExternalLink size={10} />
              </Link>
            </p>
            {complaint.followUpReason && (
              <p className="text-amber-800 text-xs mt-1 bg-white/70 rounded p-2 border border-amber-200">
                <span className="font-semibold">Reason for follow-up:</span> {complaint.followUpReason}
              </p>
            )}
            {parentComplaint && (
              <p className="text-xs text-amber-700 mt-1">
                Parent status: <span className="font-medium text-emerald-700">{parentComplaint.status}</span> · Resolved on {parentComplaint.resolvedAt ? formatDate(parentComplaint.resolvedAt) : 'N/A'}
              </p>
            )}
          </div>
        </div>
      )}

      {/* Main complaint card */}
      <div className="card space-y-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-xs font-semibold text-slate-500 bg-slate-100 px-2 py-0.5 rounded">
                {complaint.id}
              </span>
              <StatusBadge status={complaint.status} overdue={overdue} />
              {complaint.isFollowUp && (
                <span className="inline-flex items-center gap-1 text-xs font-medium text-amber-800 bg-amber-100 px-2 py-0.5 rounded-full">
                  <RotateCcw size={10} /> Follow-up
                </span>
              )}
              {isOwner && (
                <span className="text-xs text-blue-700 bg-blue-50 px-2 py-0.5 rounded-full border border-blue-100 font-medium">
                  Your Complaint
                </span>
              )}
            </div>
            <h1 className="text-lg font-bold text-slate-900 pt-1">{complaint.title}</h1>
          </div>

          <UpvoteButton
            complaintId={complaint.id}
            count={complaint.upvotes.length}
            onChange={() => setTick(t => t + 1)}
          />
        </div>

        {/* Description */}
        <div className="text-sm text-slate-700 leading-relaxed bg-slate-50 rounded-xl p-4 border border-slate-100">
          <p className="whitespace-pre-line">{complaint.description}</p>
        </div>

        {/* Photo if present */}
        {complaint.imageBase64 && (
          <div>
            <p className="text-xs font-semibold text-slate-500 mb-2">Attached Photo</p>
            <img
              src={complaint.imageBase64}
              alt="Complaint attachment"
              className="rounded-xl max-h-72 object-cover border border-slate-200"
            />
          </div>
        )}

        {/* Metadata grid */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 pt-2 border-t border-slate-100 text-xs">
          <div>
            <span className="text-slate-400 block mb-0.5 flex items-center gap-1">
              <MapPin size={12} /> Location
            </span>
            <span className="font-medium text-slate-700">
              {complaint.building}
            </span>
            <span className="text-slate-500 block">
              {complaint.floor} · {complaint.roomNumber}
            </span>
          </div>

          <div>
            <span className="text-slate-400 block mb-0.5 flex items-center gap-1">
              <Tag size={12} /> Category
            </span>
            <span className="font-medium text-slate-700">{complaint.category}</span>
          </div>

          <div>
            <span className="text-slate-400 block mb-0.5 flex items-center gap-1">
              <Calendar size={12} /> Submitted
            </span>
            <span className="font-medium text-slate-700">{formatDate(complaint.submittedAt)}</span>
            <span className="text-slate-500 block">{relativeTime(complaint.submittedAt)}</span>
          </div>

          <div>
            <span className="text-slate-400 block mb-0.5 flex items-center gap-1">
              <Clock size={12} /> Resolution
            </span>
            {complaint.status === 'resolved' && complaint.resolvedAt ? (
              <>
                <span className="font-medium text-emerald-700">
                  {formatDate(complaint.resolvedAt)}
                </span>
                <span className="text-slate-500 block">
                  {resolutionTimeDays} day{resolutionTimeDays !== 1 ? 's' : ''} to resolve
                </span>
              </>
            ) : (
              <span className="text-slate-500 italic">In progress / pending</span>
            )}
          </div>
        </div>

        {/* Staff & Resolution Remarks */}
        {(complaint.assignedStaff || complaint.resolutionRemarks) && (
          <div className="pt-2 border-t border-slate-100 space-y-2 text-xs">
            {complaint.assignedStaff && (
              <p className="text-slate-600">
                <span className="text-slate-400">Assigned Staff:</span>{' '}
                <span className="font-medium text-slate-800">{complaint.assignedStaff}</span>
              </p>
            )}
            {complaint.resolutionRemarks && (
              <div className="bg-emerald-50 border border-emerald-200 rounded-lg p-3 text-emerald-800">
                <p className="font-semibold flex items-center gap-1 mb-0.5">
                  <CheckCircle2 size={13} className="text-emerald-600" />
                  Staff Resolution Remarks:
                </p>
                <p className="leading-relaxed">{complaint.resolutionRemarks}</p>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Linked Child Follow-ups Section */}
      {followUps.length > 0 && (
        <div className="card space-y-3">
          <div className="flex items-center gap-2 text-slate-800">
            <RotateCcw size={16} className="text-blue-600" />
            <h3 className="font-bold text-sm">
              Linked Follow-up Complaints ({followUps.length})
            </h3>
          </div>
          <p className="text-xs text-slate-500">
            Follow-up tickets raised regarding recurrence or incomplete resolution of this issue:
          </p>
          <div className="divide-y divide-slate-100">
            {followUps.map(f => (
              <div key={f.id} className="py-2.5 flex items-center justify-between gap-3 text-xs">
                <div className="flex items-center gap-2 min-w-0">
                  <span className="font-mono font-medium text-slate-600">{f.id}</span>
                  <StatusBadge status={f.status} overdue={isOverdue(f)} />
                  <span className="text-slate-600 truncate max-w-xs">{f.title}</span>
                </div>
                <div className="flex items-center gap-2 flex-shrink-0">
                  <span className="text-slate-400">{formatDate(f.submittedAt)}</span>
                  <Link
                    to={`/complaints/${f.id}`}
                    className="text-blue-600 hover:text-blue-800 font-medium inline-flex items-center gap-0.5"
                  >
                    View <ArrowRight size={11} />
                  </Link>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Status History Timeline */}
      <div className="card space-y-3">
        <h3 className="font-semibold text-slate-800 text-sm flex items-center gap-2">
          <Clock size={16} className="text-slate-500" /> Lifecycle & Status History
        </h3>
        <div className="space-y-3 text-xs">
          <div className="flex gap-3">
            <div className="w-5 h-5 rounded-full bg-slate-200 flex items-center justify-center text-slate-600 flex-shrink-0 text-[10px]">
              📋
            </div>
            <div>
              <p className="font-semibold text-slate-700">Complaint Submitted</p>
              <p className="text-slate-400">{formatDate(complaint.submittedAt)}</p>
            </div>
          </div>
          {(complaint.statusHistory ?? []).map((entry, idx) => (
            <div key={idx} className="flex gap-3">
              <div className="w-5 h-5 rounded-full bg-blue-100 flex items-center justify-center text-blue-600 flex-shrink-0">
                <ArrowRight size={11} />
              </div>
              <div>
                <p className="font-semibold text-slate-700">
                  Moved to {STATUS_LABELS[entry.to]}
                </p>
                <p className="text-slate-400">
                  {formatDate(entry.changedAt)} · by {entry.changedBy}
                </p>
                {entry.remarks && (
                  <p className="text-slate-600 italic mt-0.5">"{entry.remarks}"</p>
                )}
              </div>
            </div>
          ))}
          {complaint.status === 'resolved' && (
            <div className="flex gap-3">
              <div className="w-5 h-5 rounded-full bg-emerald-100 flex items-center justify-center text-emerald-600 flex-shrink-0">
                <CheckCircle2 size={11} />
              </div>
              <div>
                <p className="font-semibold text-emerald-700">Resolved (Terminal State)</p>
                <p className="text-slate-400">
                  {complaint.resolvedAt ? formatDate(complaint.resolvedAt) : 'Completed'}
                </p>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* ── DEDICATED POST-RESOLUTION SECTION ───────────────────────────────── */}
      {isResolved && (
        <div className="card border-2 border-blue-100 bg-gradient-to-br from-white to-blue-50/30 space-y-6">
          <div className="border-b border-slate-100 pb-3 flex items-center justify-between">
            <div>
              <h2 className="text-base font-bold text-slate-900 flex items-center gap-2">
                <CheckCircle2 size={18} className="text-emerald-600" />
                Post-Resolution Options
              </h2>
              <p className="text-xs text-slate-500 mt-0.5">
                This complaint is officially resolved. As a student, you can rate the resolution or raise an independent follow-up.
              </p>
            </div>
          </div>

          {/* Feedback & Follow-up options */}
          <div className="grid md:grid-cols-2 gap-6">

            {/* ── OPTION A: Submit / View Feedback ─────────────────────────── */}
            <div className="bg-white rounded-xl p-4 border border-slate-200 shadow-sm flex flex-col justify-between space-y-4">
              <div>
                <div className="flex items-center gap-2 mb-2">
                  <Star size={16} className="text-amber-500 fill-amber-500" />
                  <h3 className="font-bold text-sm text-slate-800">Resolution Feedback</h3>
                </div>
                <p className="text-xs text-slate-500 mb-3">
                  Rate the quality of the maintenance repair work to help MAIT improve campus operations.
                </p>

                {feedbackSuccessMsg && (
                  <div className="mb-3 p-2 bg-emerald-50 border border-emerald-200 text-emerald-700 rounded text-xs">
                    {feedbackSuccessMsg}
                  </div>
                )}
                {feedbackError && (
                  <div className="mb-3 p-2 bg-red-50 border border-red-200 text-red-700 rounded text-xs">
                    {feedbackError}
                  </div>
                )}

                {/* If feedback already exists and not in editing mode: display it */}
                {complaint.feedback && !isEditingFeedback ? (
                  <div className="bg-slate-50 rounded-xl p-3.5 border border-slate-100 space-y-2">
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-1">
                        {[1, 2, 3, 4, 5].map(starVal => (
                          <Star
                            key={starVal}
                            size={16}
                            className={
                              starVal <= complaint.feedback!.rating
                                ? 'text-amber-400 fill-amber-400'
                                : 'text-slate-200'
                            }
                          />
                        ))}
                        <span className="text-xs font-bold text-slate-700 ml-1.5">
                          {complaint.feedback.rating}/5
                        </span>
                      </div>
                      <span className="text-[11px] text-slate-400">
                        {formatDate(complaint.feedback.submittedAt)}
                      </span>
                    </div>

                    {complaint.feedback.comment ? (
                      <p className="text-xs text-slate-600 italic bg-white p-2 rounded border border-slate-100">
                        "{complaint.feedback.comment}"
                      </p>
                    ) : (
                      <p className="text-xs text-slate-400 italic">No written comment provided.</p>
                    )}

                    {canSubmitFeedback && (
                      <div className="pt-1 text-right">
                        <button
                          type="button"
                          onClick={handleStartEditFeedback}
                          className="text-xs text-blue-600 hover:text-blue-800 font-medium underline"
                        >
                          Edit Feedback
                        </button>
                      </div>
                    )}
                  </div>
                ) : canSubmitFeedback ? (
                  /* Form to enter or edit feedback */
                  <form onSubmit={handleSaveFeedback} className="space-y-3">
                    <div>
                      <label className="block text-xs font-medium text-slate-700 mb-1">
                        Rating (1 to 5 stars) <span className="text-red-500">*</span>
                      </label>
                      <div className="flex items-center gap-1.5">
                        {[1, 2, 3, 4, 5].map(s => {
                          const active = (hoverRating || rating) >= s;
                          return (
                            <button
                              type="button"
                              key={s}
                              onClick={() => setRating(s)}
                              onMouseEnter={() => setHoverRating(s)}
                              onMouseLeave={() => setHoverRating(0)}
                              className="p-1 transition-transform hover:scale-110 focus:outline-none"
                              aria-label={`Rate ${s} star${s > 1 ? 's' : ''}`}
                            >
                              <Star
                                size={22}
                                className={active ? 'text-amber-400 fill-amber-400' : 'text-slate-300'}
                              />
                            </button>
                          );
                        })}
                        <span className="text-xs font-semibold text-slate-600 ml-2">
                          {rating === 1 && '1 - Poor'}
                          {rating === 2 && '2 - Fair'}
                          {rating === 3 && '3 - Good'}
                          {rating === 4 && '4 - Very Good'}
                          {rating === 5 && '5 - Excellent'}
                        </span>
                      </div>
                    </div>

                    <div>
                      <label className="block text-xs font-medium text-slate-700 mb-1">
                        Comments (optional)
                      </label>
                      <textarea
                        className="input resize-none text-xs"
                        rows={3}
                        placeholder="Share any comments regarding the maintenance work done..."
                        value={comment}
                        onChange={e => setComment(e.target.value)}
                        maxLength={500}
                      />
                    </div>

                    <div className="flex gap-2">
                      <button
                        type="submit"
                        disabled={rating < 1}
                        className="btn-primary text-xs py-1.5 px-3 flex-1"
                      >
                        {complaint.feedback ? 'Update Feedback' : 'Submit Feedback'}
                      </button>
                      {isEditingFeedback && (
                        <button
                          type="button"
                          onClick={() => setIsEditingFeedback(false)}
                          className="btn-secondary text-xs py-1.5 px-3"
                        >
                          Cancel
                        </button>
                      )}
                    </div>
                  </form>
                ) : (
                  <p className="text-xs text-slate-400 italic">
                    Feedback can only be submitted by the student who filed this complaint.
                  </p>
                )}
              </div>
            </div>

            {/* ── OPTION B: Raise a Follow-up Complaint ─────────────────────── */}
            <div className="bg-white rounded-xl p-4 border border-slate-200 shadow-sm flex flex-col justify-between space-y-4">
              <div>
                <div className="flex items-center gap-2 mb-2">
                  <RotateCcw size={16} className="text-blue-600" />
                  <h3 className="font-bold text-sm text-slate-800">Raise a Follow-up Complaint</h3>
                </div>
                <p className="text-xs text-slate-500 mb-3">
                  Did the issue recur or was it not properly fixed? Raise an independent follow-up ticket with location and details pre-linked.
                </p>

                {/* Safeguard: duplicate active follow-up */}
                {activeFollowUp ? (
                  <div className="p-3 bg-amber-50 border border-amber-200 rounded-lg space-y-1.5">
                    <div className="flex items-center gap-1.5 text-xs font-semibold text-amber-900">
                      <ShieldAlert size={14} className="text-amber-600" />
                      Active Follow-up in Progress
                    </div>
                    <p className="text-xs text-amber-800">
                      An active follow-up (<span className="font-mono font-medium">{activeFollowUp.id}</span>) is already being addressed. You cannot submit another follow-up until it is resolved.
                    </p>
                    <Link
                      to={`/complaints/${activeFollowUp.id}`}
                      className="text-xs text-blue-700 font-medium hover:underline inline-flex items-center gap-1 mt-1"
                    >
                      View Active Follow-up ({activeFollowUp.id}) →
                    </Link>
                  </div>
                ) : !canRaiseFollowUp ? (
                  <div className="p-3 bg-slate-50 border border-slate-200 rounded-lg text-xs text-slate-500">
                    <p>
                      Follow-up complaints can only be raised by the original student author.
                    </p>
                  </div>
                ) : (
                  <div className="space-y-3">
                    <div className="text-xs text-slate-600 bg-slate-50 p-2.5 rounded-lg border border-slate-100">
                      <p className="font-semibold text-slate-700 mb-0.5">How follow-ups work:</p>
                      <ul className="list-disc list-inside space-y-0.5 text-slate-500">
                        <li>Original ticket remains permanently <span className="font-medium text-emerald-700">Resolved</span>.</li>
                        <li>Generates a new ticket with independent SLA & workflow.</li>
                        <li>Location and category are automatically carried over.</li>
                      </ul>
                    </div>

                    <button
                      type="button"
                      onClick={handleOpenFollowUpForm}
                      className="btn-primary w-full text-xs py-2 flex items-center justify-center gap-1.5"
                    >
                      <RotateCcw size={14} /> Raise a Follow-up Complaint
                    </button>
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* ── Follow-up Complaint Creation Modal / Form ─────────────────────────── */}
      {showFollowUpForm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50 backdrop-blur-sm animate-in fade-in">
          <div className="bg-white rounded-2xl max-w-xl w-full max-h-[90vh] overflow-y-auto p-6 shadow-2xl border border-slate-100 space-y-5">
            <div className="flex items-start justify-between gap-3 border-b border-slate-100 pb-3">
              <div>
                <h3 className="text-base font-bold text-slate-900 flex items-center gap-2">
                  <RotateCcw size={16} className="text-blue-600" />
                  Raise Follow-up Complaint
                </h3>
                <p className="text-xs text-slate-500 mt-0.5">
                  Linked to original complaint <span className="font-mono text-slate-700 font-semibold">{complaint.id}</span>
                </p>
              </div>
              <button
                type="button"
                onClick={() => setShowFollowUpForm(false)}
                className="p-1 rounded-lg text-slate-400 hover:text-slate-600 hover:bg-slate-100"
              >
                <X size={18} />
              </button>
            </div>

            {followUpError && (
              <div className="p-3 bg-red-50 border border-red-200 text-red-700 text-xs rounded-lg flex items-start gap-2">
                <AlertTriangle size={14} className="text-red-500 flex-shrink-0 mt-0.5" />
                <span>{followUpError}</span>
              </div>
            )}

            <form onSubmit={handleSubmitFollowUp} className="space-y-4">
              {/* Prefilled Location Badges (Read-only guarantee) */}
              <div className="bg-slate-50 p-3 rounded-xl border border-slate-200 space-y-1.5 text-xs">
                <p className="font-semibold text-slate-700">Prefilled Location & Category (inherited):</p>
                <div className="flex flex-wrap gap-2 text-slate-600">
                  <span className="bg-white px-2.5 py-1 rounded-md border border-slate-200">
                    🏢 {complaint.building}
                  </span>
                  <span className="bg-white px-2.5 py-1 rounded-md border border-slate-200">
                    📍 {complaint.floor} · {complaint.roomNumber}
                  </span>
                  <span className="bg-white px-2.5 py-1 rounded-md border border-slate-200">
                    🏷️ {complaint.category}
                  </span>
                </div>
              </div>

              <div>
                <label className="block text-xs font-medium text-slate-700 mb-1">
                  Follow-up Complaint Title
                </label>
                <input
                  type="text"
                  className="input text-xs"
                  value={followUpTitle}
                  onChange={e => setFollowUpTitle(e.target.value)}
                  required
                />
              </div>

              {/* Required follow-up reason */}
              <div>
                <label className="block text-xs font-medium text-slate-700 mb-1">
                  Why does the issue remain unresolved or how has it returned? <span className="text-red-500">*</span>
                </label>
                <textarea
                  className="input resize-none text-xs"
                  rows={4}
                  placeholder="Explain clearly what happened after the resolution (e.g., 'The tap was repaired on Monday but began leaking again under water pressure this morning...')"
                  value={followUpReason}
                  onChange={e => setFollowUpReason(e.target.value)}
                  required
                />
                <p className="text-[11px] text-slate-400 mt-1">
                  Required: Minimum 10 characters explaining the recurrence.
                </p>
              </div>

              {/* Optional Photo Attachment */}
              <div>
                <label className="block text-xs font-medium text-slate-700 mb-1">
                  Attach Updated Photo (Optional)
                </label>
                {followUpImagePreview ? (
                  <div className="relative w-full max-w-xs">
                    <img
                      src={followUpImagePreview}
                      alt="Follow up photo"
                      className="rounded-lg max-h-36 object-cover border border-slate-200"
                    />
                    <button
                      type="button"
                      onClick={() => {
                        setFollowUpImage(undefined);
                        setFollowUpImagePreview(undefined);
                      }}
                      className="absolute top-1 right-1 bg-black/60 hover:bg-black/80 text-white rounded-full p-1"
                    >
                      <X size={12} />
                    </button>
                  </div>
                ) : (
                  <button
                    type="button"
                    onClick={() => fileRef.current?.click()}
                    className="flex items-center justify-center gap-2 w-full border border-dashed border-slate-300 rounded-lg py-4 text-xs text-slate-500 hover:border-blue-400 hover:bg-blue-50/50"
                  >
                    <Upload size={14} /> Click to upload photo (PNG, JPG up to 5MB)
                  </button>
                )}
                <input
                  ref={fileRef}
                  type="file"
                  accept="image/*"
                  className="hidden"
                  onChange={handleImageUpload}
                />
              </div>

              {/* Notice */}
              <div className="p-3 bg-blue-50 border border-blue-100 rounded-lg text-[11px] text-blue-700">
                Notice: Submitting will create a fresh complaint with its own independent timeline. The original complaint <span className="font-mono font-semibold">{complaint.id}</span> will remain permanently resolved.
              </div>

              {/* Actions */}
              <div className="flex gap-2 justify-end pt-2">
                <button
                  type="button"
                  onClick={() => setShowFollowUpForm(false)}
                  className="btn-secondary text-xs"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={followUpReason.trim().length < 10}
                  className="btn-primary text-xs"
                >
                  Submit Follow-up Complaint
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
