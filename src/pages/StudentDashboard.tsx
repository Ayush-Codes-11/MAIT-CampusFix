import { useMemo, useState } from 'react';
import { getComplaints } from '../utils/storage';
import { useAuth } from '../context/AuthContext';
import {
  groupByStatus,
  groupByBuilding,
  isOverdue,
  relativeTime,
  avgResolutionDays,
} from '../utils/helpers';
import { StatusBadge } from '../components/shared/StatusBadge';
import {
  ClipboardList,
  Clock,
  Loader2,
  CheckCircle2,
  AlertTriangle,
  TrendingUp,
  MapPin,
  RefreshCw,
  RotateCcw,
} from 'lucide-react';
import { Link } from 'react-router-dom';

function StatCard({
  label,
  value,
  icon: Icon,
  colorClass,
  sub,
}: {
  label: string;
  value: number | string;
  icon: React.FC<{ size?: number; className?: string }>;
  colorClass: string;
  sub?: string;
}) {
  return (
    <div className="card flex items-center gap-4">
      <div className={`w-11 h-11 rounded-xl flex items-center justify-center flex-shrink-0 ${colorClass}`}>
        <Icon size={22} className="text-white" />
      </div>
      <div>
        <p className="text-2xl font-bold text-slate-800">{value}</p>
        <p className="text-sm text-slate-500">{label}</p>
        {sub && <p className="text-xs text-slate-400 mt-0.5">{sub}</p>}
      </div>
    </div>
  );
}

export default function StudentDashboard() {
  const { user } = useAuth();
  const [tick, setTick] = useState(0);
  const complaints = useMemo(() => getComplaints(), [tick]);

  const byStatus = groupByStatus(complaints);
  const byBuilding = groupByBuilding(complaints);
  const overdueCount = complaints.filter(isOverdue).length;
  const avg = avgResolutionDays(complaints);
  const recent = [...complaints]
    .sort((a, b) => new Date(b.submittedAt).getTime() - new Date(a.submittedAt).getTime())
    .slice(0, 5);

  const displayName = user?.name || user?.email || 'Student';

  return (
    <div className="max-w-6xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-xl font-bold text-slate-800">Student Dashboard</h2>
          <p className="text-sm text-slate-500 mt-0.5">
            Welcome back, <span className="font-medium text-slate-700">{displayName}</span>
          </p>
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => setTick(t => t + 1)}
            className="btn-ghost flex items-center gap-1.5 text-sm"
          >
            <RefreshCw size={14} />
            Refresh
          </button>
          <Link to="/report" className="btn-primary text-sm flex items-center gap-1.5">
            <span>+ Report Issue</span>
          </Link>
        </div>
      </div>

      {/* Stat cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard
          label="Total Complaints"
          value={complaints.length}
          icon={ClipboardList}
          colorClass="bg-slate-700"
        />
        <StatCard
          label="Pending"
          value={byStatus.pending.length}
          icon={Clock}
          colorClass="bg-amber-500"
        />
        <StatCard
          label="In Progress"
          value={byStatus['in-progress'].length}
          icon={Loader2}
          colorClass="bg-blue-500"
        />
        <StatCard
          label="Resolved"
          value={byStatus.resolved.length}
          icon={CheckCircle2}
          colorClass="bg-emerald-500"
          sub={avg !== null ? `Avg ${avg}d resolution` : undefined}
        />
      </div>

      {/* Overdue alert */}
      {overdueCount > 0 && (
        <div className="flex items-center gap-3 bg-red-50 border border-red-200 rounded-xl px-4 py-3">
          <AlertTriangle size={18} className="text-red-500 flex-shrink-0" />
          <p className="text-sm text-red-700">
            <span className="font-semibold">{overdueCount} complaint{overdueCount > 1 ? 's are' : ' is'} overdue</span>
            {' '}and need immediate attention.{' '}
            <Link to="/analytics" className="underline hover:no-underline">View in Analytics →</Link>
          </p>
        </div>
      )}

      <div className="grid lg:grid-cols-5 gap-6">
        {/* Recent complaints */}
        <div className="lg:col-span-3 space-y-3">
          <div className="flex items-center justify-between">
            <h3 className="font-semibold text-slate-700 flex items-center gap-2">
              <TrendingUp size={16} />
              Recent Complaints
            </h3>
            <Link to="/complaints" className="text-sm text-blue-600 hover:underline">
              View all →
            </Link>
          </div>
          <div className="space-y-2">
            {recent.length === 0 ? (
              <div className="card py-8 text-center text-slate-400 text-sm">
                No complaints recorded yet. Click "+ Report Issue" to submit one.
              </div>
            ) : (
              recent.map(c => (
                <Link
                  key={c.id}
                  to={`/complaints/${c.id}`}
                  className="card py-3 flex items-start justify-between gap-3 hover:shadow-md transition-shadow block hover:border-blue-200"
                >
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-1">
                      <span className="text-xs font-mono text-slate-400">{c.id}</span>
                      <StatusBadge status={c.status} overdue={isOverdue(c)} />
                      {c.isFollowUp && (
                        <span className="inline-flex items-center gap-1 text-[10px] font-medium text-amber-800 bg-amber-100 px-1.5 py-0.2 rounded-full">
                          <RotateCcw size={9} /> Follow-up
                        </span>
                      )}
                    </div>
                    <p className="text-sm font-medium text-slate-800 leading-snug truncate hover:text-blue-600 transition-colors">
                      {c.title}
                    </p>
                    <p className="text-xs text-slate-500 mt-0.5 flex items-center gap-1">
                      <MapPin size={11} />
                      {c.building} · {relativeTime(c.submittedAt)}
                    </p>
                  </div>
                  <span className="text-xs text-slate-400 flex-shrink-0">
                    👍 {c.upvotes.length}
                  </span>
                </Link>
              ))
            )}
          </div>
        </div>

        {/* Building-wise overview */}
        <div className="lg:col-span-2">
          <h3 className="font-semibold text-slate-700 flex items-center gap-2 mb-3">
            <MapPin size={16} />
            Building Overview
          </h3>
          <div className="card p-0 overflow-hidden divide-y divide-slate-50">
            {Object.entries(byBuilding)
              .sort((a, b) => b[1].length - a[1].length)
              .map(([building, cs]) => {
                const pending = cs.filter(c => c.status === 'pending').length;
                const inProg = cs.filter(c => c.status === 'in-progress').length;
                const resolved = cs.filter(c => c.status === 'resolved').length;
                return (
                  <div key={building} className="px-4 py-3">
                    <p className="text-sm font-medium text-slate-700 truncate mb-1.5">
                      {building}
                    </p>
                    <div className="flex items-center gap-1.5 flex-wrap">
                      {pending > 0 && (
                        <span className="text-xs bg-amber-100 text-amber-700 px-2 py-0.5 rounded-full">
                          {pending} pending
                        </span>
                      )}
                      {inProg > 0 && (
                        <span className="text-xs bg-blue-100 text-blue-700 px-2 py-0.5 rounded-full">
                          {inProg} in-progress
                        </span>
                      )}
                      {resolved > 0 && (
                        <span className="text-xs bg-emerald-100 text-emerald-700 px-2 py-0.5 rounded-full">
                          {resolved} resolved
                        </span>
                      )}
                    </div>
                  </div>
                );
              })}
          </div>
        </div>
      </div>
    </div>
  );
}
