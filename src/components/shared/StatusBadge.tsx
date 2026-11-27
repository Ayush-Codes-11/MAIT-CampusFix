import type { ComplaintStatus } from '../../types';
import { Clock, Loader2, CheckCircle2, AlertTriangle } from 'lucide-react';

interface StatusBadgeProps {
  status: ComplaintStatus;
  overdue?: boolean;
  className?: string;
}

const CONFIG: Record<ComplaintStatus, { label: string; className: string; Icon: React.FC<{ size?: number }> }> = {
  pending: {
    label: 'Pending',
    className: 'badge-pending',
    Icon: Clock,
  },
  'in-progress': {
    label: 'In Progress',
    className: 'badge-inprogress',
    Icon: Loader2,
  },
  resolved: {
    label: 'Resolved',
    className: 'badge-resolved',
    Icon: CheckCircle2,
  },
};

export function StatusBadge({ status, overdue = false, className = '' }: StatusBadgeProps) {
  if (overdue && status !== 'resolved') {
    return (
      <span className={`badge-overdue ${className}`}>
        <AlertTriangle size={12} />
        Overdue
      </span>
    );
  }

  const { label, className: baseClass, Icon } = CONFIG[status];
  return (
    <span className={`${baseClass} ${className}`}>
      <Icon size={12} />
      {label}
    </span>
  );
}
