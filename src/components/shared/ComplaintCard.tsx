import { StatusBadge } from './StatusBadge';
import { UpvoteButton } from './UpvoteButton';
import { MapPin, Calendar, Tag, RotateCcw, Star, ArrowRight } from 'lucide-react';
import type { Complaint } from '../../types';
import { isOverdue, formatDate, relativeTime } from '../../utils/helpers';
import { Link } from 'react-router-dom';

interface ComplaintCardProps {
  complaint: Complaint;
  showAdmin?: boolean;
  onUpdate?: () => void;
}

export function ComplaintCard({ complaint, showAdmin = false, onUpdate }: ComplaintCardProps) {
  const overdue = isOverdue(complaint);

  return (
    <div className="card hover:shadow-md transition-shadow duration-150">
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex flex-wrap items-center gap-2 mb-1.5">
            <span className="text-xs font-mono text-slate-400">{complaint.id}</span>
            <StatusBadge status={complaint.status} overdue={overdue} />
            {complaint.isFollowUp && (
              <span className="inline-flex items-center gap-1 text-[11px] font-medium text-amber-800 bg-amber-100 px-2 py-0.5 rounded-full">
                <RotateCcw size={10} /> Follow-up
                {complaint.originalComplaintId && ` to ${complaint.originalComplaintId}`}
              </span>
            )}
            {complaint.feedback && (
              <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-amber-700 bg-amber-50 px-2 py-0.5 rounded border border-amber-200">
                <Star size={10} className="fill-amber-400 text-amber-400" />
                {complaint.feedback.rating}/5
              </span>
            )}
            {overdue && (
              <span className="text-xs text-red-500 font-medium">
                {Math.floor((Date.now() - new Date(complaint.submittedAt).getTime()) / 86_400_000)}d old
              </span>
            )}
          </div>

          <h3 className="font-semibold text-slate-800 text-sm leading-snug mb-1">
            <Link
              to={`/complaints/${complaint.id}`}
              className="hover:text-blue-600 transition-colors"
            >
              {complaint.title}
            </Link>
          </h3>

          <p className="text-sm text-slate-500 leading-relaxed line-clamp-2 mb-3">
            {complaint.description}
          </p>

          <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-500">
            <span className="flex items-center gap-1">
              <MapPin size={12} />
              {complaint.building} · {complaint.floor} · {complaint.roomNumber}
            </span>
            <span className="flex items-center gap-1">
              <Tag size={12} />
              {complaint.category}
            </span>
            <span className="flex items-center gap-1">
              <Calendar size={12} />
              <span title={formatDate(complaint.submittedAt)}>{relativeTime(complaint.submittedAt)}</span>
            </span>
          </div>

          {complaint.assignedStaff && (
            <p className="mt-2 text-xs text-slate-400">
              Assigned to: <span className="text-slate-600 font-medium">{complaint.assignedStaff}</span>
            </p>
          )}

          {complaint.resolutionRemarks && (
            <p className="mt-2 text-xs text-emerald-700 bg-emerald-50 rounded px-2 py-1 border border-emerald-100">
              ✓ {complaint.resolutionRemarks}
            </p>
          )}

          <div className="mt-3 pt-2 border-t border-slate-50 flex items-center justify-between text-xs">
            <Link
              to={`/complaints/${complaint.id}`}
              className="text-blue-600 hover:text-blue-800 font-medium inline-flex items-center gap-1"
            >
              View Details & History <ArrowRight size={12} />
            </Link>
          </div>
        </div>

        <div className="flex flex-col items-end gap-2 flex-shrink-0">
          <UpvoteButton
            complaintId={complaint.id}
            count={complaint.upvotes.length}
            onChange={onUpdate}
          />
          {showAdmin && (
            <Link
              to={`/admin?id=${complaint.id}`}
              className="text-xs text-blue-600 hover:underline"
            >
              Manage
            </Link>
          )}
        </div>
      </div>
    </div>
  );
}
