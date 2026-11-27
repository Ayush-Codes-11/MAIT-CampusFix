import { useMemo } from 'react';
import { getComplaints } from '../utils/storage';
import {
  groupByStatus,
  groupByBuilding,
  avgResolutionDays,
  medianResolutionDays,
  isOverdue,
  daysBetween,
  formatDate,
} from '../utils/helpers';
import { StatusBadge } from '../components/shared/StatusBadge';
import {
  BarChart3,
  AlertTriangle,
  Clock,
  TrendingUp,
  CheckCircle2,
  RotateCcw,
  Star,
  FileText,
} from 'lucide-react';
import { BUILDINGS } from '../types';

function MetricCard({
  label,
  value,
  sub,
  icon: Icon,
  colorClass,
}: {
  label: string;
  value: string | number;
  sub?: string;
  icon: React.FC<{ size?: number }>;
  colorClass: string;
}) {
  return (
    <div className="card">
      <div className={`w-9 h-9 rounded-lg flex items-center justify-center mb-3 ${colorClass}`}>
        <Icon size={18} />
      </div>
      <p className="text-2xl font-bold text-slate-800">{value}</p>
      <p className="text-sm text-slate-500 mt-0.5">{label}</p>
      {sub && <p className="text-xs text-slate-400 mt-1">{sub}</p>}
    </div>
  );
}

function BarRow({
  label,
  value,
  max,
  colorClass,
}: {
  label: string;
  value: number;
  max: number;
  colorClass: string;
}) {
  const pct = max > 0 ? Math.round((value / max) * 100) : 0;
  return (
    <div>
      <div className="flex justify-between text-xs text-slate-600 mb-1">
        <span className="truncate">{label}</span>
        <span className="font-medium ml-2">{value}</span>
      </div>
      <div className="h-2 bg-slate-100 rounded-full overflow-hidden">
        <div
          className={`h-full rounded-full transition-all duration-500 ${colorClass}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

export default function AnalyticsPage() {
  const complaints = useMemo(() => getComplaints(), []);
  const byStatus = groupByStatus(complaints);
  const byBuilding = groupByBuilding(complaints);
  const overdue = complaints.filter(isOverdue);
  const avg = avgResolutionDays(complaints);
  const median = medianResolutionDays(complaints);

  // Original vs Follow-up breakdown
  const followUpComplaints = complaints.filter(c => c.isFollowUp);
  const originalComplaints = complaints.filter(c => !c.isFollowUp);
  const followUpRate =
    originalComplaints.length > 0
      ? Math.round((followUpComplaints.length / originalComplaints.length) * 100)
      : 0;

  // Feedback analysis
  const complaintsWithFeedback = complaints.filter(c => c.feedback?.rating);
  const avgFeedbackScore =
    complaintsWithFeedback.length > 0
      ? (
          complaintsWithFeedback.reduce((acc, c) => acc + c.feedback!.rating, 0) /
          complaintsWithFeedback.length
        ).toFixed(1)
      : null;

  // Independent resolution times
  const avgOriginalResDays = avgResolutionDays(originalComplaints);
  const avgFollowUpResDays = avgResolutionDays(followUpComplaints);

  // Category breakdown
  const byCategoryMap: Record<string, number> = {};
  complaints.forEach(c => {
    byCategoryMap[c.category] = (byCategoryMap[c.category] ?? 0) + 1;
  });
  const categoryEntries = Object.entries(byCategoryMap).sort((a, b) => b[1] - a[1]);

  // Building trends (total vs pending)
  const buildingRows = BUILDINGS.map(b => ({
    name: b,
    total: byBuilding[b]?.length ?? 0,
    pending: byBuilding[b]?.filter(c => c.status === 'pending').length ?? 0,
    inProgress: byBuilding[b]?.filter(c => c.status === 'in-progress').length ?? 0,
    resolved: byBuilding[b]?.filter(c => c.status === 'resolved').length ?? 0,
  })).filter(r => r.total > 0).sort((a, b) => b.total - a.total);

  const maxBuilding = Math.max(...buildingRows.map(r => r.total), 1);
  const maxCategory = Math.max(...categoryEntries.map(e => e[1]), 1);

  return (
    <div className="max-w-5xl mx-auto space-y-6">
      <div className="flex items-center gap-2">
        <BarChart3 size={22} className="text-blue-600" />
        <div>
          <h2 className="text-xl font-bold text-slate-800">Analytics</h2>
          <p className="text-sm text-slate-500">Overview of campus maintenance health & accountability</p>
        </div>
      </div>

      {/* Key metrics */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <MetricCard
          label="Total Complaints"
          value={complaints.length}
          sub={`${originalComplaints.length} original · ${followUpComplaints.length} follow-up`}
          icon={BarChart3}
          colorClass="bg-slate-100 text-slate-600"
        />
        <MetricCard
          label="Pending"
          value={byStatus.pending.length}
          sub={`${Math.round((byStatus.pending.length / (complaints.length || 1)) * 100)}% of total`}
          icon={Clock}
          colorClass="bg-amber-100 text-amber-600"
        />
        <MetricCard
          label="Resolved"
          value={byStatus.resolved.length}
          sub={`${Math.round((byStatus.resolved.length / (complaints.length || 1)) * 100)}% of total`}
          icon={CheckCircle2}
          colorClass="bg-emerald-100 text-emerald-600"
        />
        <MetricCard
          label="Overdue"
          value={overdue.length}
          sub="Past SLA threshold"
          icon={AlertTriangle}
          colorClass="bg-red-100 text-red-600"
        />
      </div>

      {/* Post-Resolution & Follow-up Analytics Row */}
      <div className="grid md:grid-cols-2 gap-4">
        {/* Follow-up & Recurrence Accountability Card */}
        <div className="card space-y-4">
          <h3 className="font-semibold text-slate-800 text-sm flex items-center gap-2">
            <RotateCcw size={16} className="text-blue-600" />
            Follow-up & Recurrence Monitoring
          </h3>
          <div className="grid grid-cols-2 gap-3 text-center">
            <div className="bg-slate-50 p-3 rounded-xl border border-slate-100">
              <p className="text-xl font-bold text-slate-800">{followUpComplaints.length}</p>
              <p className="text-xs text-slate-500 mt-0.5">Follow-up Complaints</p>
            </div>
            <div className="bg-slate-50 p-3 rounded-xl border border-slate-100">
              <p className="text-xl font-bold text-slate-800">{followUpRate}%</p>
              <p className="text-xs text-slate-500 mt-0.5">Recurrence Rate</p>
            </div>
          </div>
          <div className="space-y-2 text-xs text-slate-600">
            <div className="flex justify-between py-1.5 border-b border-slate-50">
              <span>Original complaints</span>
              <span className="font-semibold text-slate-800">{originalComplaints.length}</span>
            </div>
            <div className="flex justify-between py-1.5 border-b border-slate-50">
              <span>Follow-ups currently pending</span>
              <span className="font-semibold text-amber-600">
                {followUpComplaints.filter(c => c.status === 'pending').length}
              </span>
            </div>
            <div className="flex justify-between py-1.5">
              <span>Follow-ups successfully resolved</span>
              <span className="font-semibold text-emerald-600">
                {followUpComplaints.filter(c => c.status === 'resolved').length}
              </span>
            </div>
          </div>
        </div>

        {/* Student Resolution Feedback Card */}
        <div className="card space-y-4">
          <h3 className="font-semibold text-slate-800 text-sm flex items-center gap-2">
            <Star size={16} className="text-amber-500 fill-amber-500" />
            Student Resolution Satisfaction
          </h3>
          <div className="grid grid-cols-2 gap-3 text-center">
            <div className="bg-amber-50/60 p-3 rounded-xl border border-amber-200">
              <p className="text-xl font-bold text-amber-900">
                {avgFeedbackScore ? `${avgFeedbackScore} ★` : '—'}
              </p>
              <p className="text-xs text-amber-700 mt-0.5">Avg Student Rating</p>
            </div>
            <div className="bg-slate-50 p-3 rounded-xl border border-slate-100">
              <p className="text-xl font-bold text-slate-800">{complaintsWithFeedback.length}</p>
              <p className="text-xs text-slate-500 mt-0.5">Feedback Responses</p>
            </div>
          </div>
          <div className="space-y-2 text-xs text-slate-600">
            <p className="text-slate-500 italic">
              Students can rate resolved maintenance tasks from 1 to 5 stars. Feedback is saved to original records for quality tracking.
            </p>
          </div>
        </div>
      </div>

      {/* Resolution time */}
      <div className="grid md:grid-cols-2 gap-4">
        <div className="card">
          <h3 className="font-semibold text-slate-700 mb-4 flex items-center gap-2">
            <TrendingUp size={16} />
            Resolution Time (Independent per complaint)
          </h3>
          <div className="space-y-3">
            <div className="flex justify-between items-center py-2 border-b border-slate-50">
              <span className="text-sm text-slate-600">Overall average resolution</span>
              <span className="font-bold text-slate-800">
                {avg !== null ? `${avg} days` : '—'}
              </span>
            </div>
            <div className="flex justify-between items-center py-2 border-b border-slate-50">
              <span className="text-sm text-slate-600">Overall median resolution</span>
              <span className="font-bold text-slate-800">
                {median !== null ? `${median} days` : '—'}
              </span>
            </div>
            <div className="flex justify-between items-center py-2 border-b border-slate-50">
              <span className="text-sm text-slate-600">Original complaints avg</span>
              <span className="font-bold text-slate-800">
                {avgOriginalResDays !== null ? `${avgOriginalResDays} days` : '—'}
              </span>
            </div>
            <div className="flex justify-between items-center py-2">
              <span className="text-sm text-slate-600">Follow-up complaints avg</span>
              <span className="font-bold text-slate-800">
                {avgFollowUpResDays !== null ? `${avgFollowUpResDays} days` : '—'}
              </span>
            </div>
          </div>
          <p className="text-xs text-slate-400 mt-3">
            * Resolution time is calculated independently for each complaint record from its submission to resolution date.
          </p>
        </div>

        {/* Status donut summary */}
        <div className="card">
          <h3 className="font-semibold text-slate-700 mb-4">Status Breakdown</h3>
          <div className="space-y-3">
            {[
              { label: 'Pending',     count: byStatus.pending.length,       color: 'bg-amber-400' },
              { label: 'In Progress', count: byStatus['in-progress'].length, color: 'bg-blue-400' },
              { label: 'Resolved',    count: byStatus.resolved.length,       color: 'bg-emerald-400' },
            ].map(row => (
              <BarRow
                key={row.label}
                label={row.label}
                value={row.count}
                max={complaints.length}
                colorClass={row.color}
              />
            ))}
          </div>
        </div>
      </div>

      {/* Overdue complaints */}
      {overdue.length > 0 && (
        <div className="card">
          <h3 className="font-semibold text-slate-700 mb-4 flex items-center gap-2 text-red-600">
            <AlertTriangle size={16} />
            Overdue Complaints ({overdue.length})
          </h3>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-slate-500 border-b border-slate-100">
                  <th className="text-left pb-2 font-medium">ID</th>
                  <th className="text-left pb-2 font-medium">Title</th>
                  <th className="text-left pb-2 font-medium">Building</th>
                  <th className="text-left pb-2 font-medium">Submitted</th>
                  <th className="text-left pb-2 font-medium">Age</th>
                  <th className="text-left pb-2 font-medium">Status</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-50">
                {overdue
                  .sort((a, b) => daysBetween(b.submittedAt) - daysBetween(a.submittedAt))
                  .map(c => (
                    <tr key={c.id} className="hover:bg-red-50 transition-colors">
                      <td className="py-2 font-mono text-xs text-slate-500">
                        {c.id}
                        {c.isFollowUp && (
                          <span className="block text-[10px] text-amber-700 font-semibold">
                            Follow-up
                          </span>
                        )}
                      </td>
                      <td className="py-2 font-medium text-slate-700 max-w-xs truncate">{c.title}</td>
                      <td className="py-2 text-slate-500 text-xs">{c.building}</td>
                      <td className="py-2 text-slate-500 text-xs">{formatDate(c.submittedAt)}</td>
                      <td className="py-2 text-red-600 font-semibold">{daysBetween(c.submittedAt)}d</td>
                      <td className="py-2"><StatusBadge status={c.status} overdue /></td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Building-wise trends */}
      <div className="card">
        <h3 className="font-semibold text-slate-700 mb-4 flex items-center gap-2">
          🏢 Building-wise Trends
        </h3>
        <div className="space-y-3">
          {buildingRows.map(row => (
            <div key={row.name}>
              <div className="flex justify-between text-xs text-slate-600 mb-1">
                <span className="font-medium truncate">{row.name}</span>
                <span className="ml-2 flex gap-2 flex-shrink-0">
                  <span className="text-amber-600">{row.pending}p</span>
                  <span className="text-blue-600">{row.inProgress}ip</span>
                  <span className="text-emerald-600">{row.resolved}r</span>
                </span>
              </div>
              <div className="h-2.5 bg-slate-100 rounded-full overflow-hidden flex">
                <div className="h-full bg-amber-400" style={{ width: `${(row.pending / maxBuilding) * 100}%` }} />
                <div className="h-full bg-blue-400" style={{ width: `${(row.inProgress / maxBuilding) * 100}%` }} />
                <div className="h-full bg-emerald-400" style={{ width: `${(row.resolved / maxBuilding) * 100}%` }} />
              </div>
            </div>
          ))}
        </div>
        <div className="flex gap-4 mt-3 text-xs text-slate-500">
          <span className="flex items-center gap-1"><span className="w-3 h-1.5 rounded bg-amber-400 inline-block" /> Pending</span>
          <span className="flex items-center gap-1"><span className="w-3 h-1.5 rounded bg-blue-400 inline-block" /> In Progress</span>
          <span className="flex items-center gap-1"><span className="w-3 h-1.5 rounded bg-emerald-400 inline-block" /> Resolved</span>
        </div>
      </div>

      {/* Category breakdown */}
      <div className="card">
        <h3 className="font-semibold text-slate-700 mb-4">Category Distribution</h3>
        <div className="space-y-3">
          {categoryEntries.map(([cat, count]) => (
            <BarRow key={cat} label={cat} value={count} max={maxCategory} colorClass="bg-blue-500" />
          ))}
        </div>
      </div>
    </div>
  );
}
