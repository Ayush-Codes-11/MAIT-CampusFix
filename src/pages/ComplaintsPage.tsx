import { useState, useMemo } from 'react';
import { getComplaints } from '../utils/storage';
import { ComplaintCard } from '../components/shared/ComplaintCard';
import { BUILDINGS, ISSUE_CATEGORIES, type ComplaintStatus, type IssueCategory } from '../types';
import { Search, Filter } from 'lucide-react';

const STATUS_OPTIONS: { value: '' | ComplaintStatus; label: string }[] = [
  { value: '', label: 'All Statuses' },
  { value: 'pending', label: 'Pending' },
  { value: 'in-progress', label: 'In Progress' },
  { value: 'resolved', label: 'Resolved' },
];

export default function ComplaintsPage() {
  const [tick, setTick] = useState(0);
  const [search, setSearch] = useState('');
  const [filterStatus, setFilterStatus] = useState<'' | ComplaintStatus>('');
  const [filterBuilding, setFilterBuilding] = useState('');
  const [filterCategory, setFilterCategory] = useState<'' | IssueCategory>('');
  const [filterType, setFilterType] = useState<'' | 'original' | 'followup'>('');
  const [sortBy, setSortBy] = useState<'newest' | 'oldest' | 'upvotes'>('newest');

  const complaints = useMemo(() => getComplaints(), [tick]);

  const filtered = useMemo(() => {
    let result = complaints;

    if (search.trim()) {
      const q = search.toLowerCase();
      result = result.filter(
        c =>
          c.title.toLowerCase().includes(q) ||
          c.description.toLowerCase().includes(q) ||
          c.id.toLowerCase().includes(q) ||
          c.building.toLowerCase().includes(q)
      );
    }

    if (filterStatus) result = result.filter(c => c.status === filterStatus);
    if (filterBuilding) result = result.filter(c => c.building === filterBuilding);
    if (filterCategory) result = result.filter(c => c.category === filterCategory);
    if (filterType === 'original') result = result.filter(c => !c.isFollowUp);
    if (filterType === 'followup') result = result.filter(c => c.isFollowUp);

    if (sortBy === 'newest') {
      result = [...result].sort(
        (a, b) => new Date(b.submittedAt).getTime() - new Date(a.submittedAt).getTime()
      );
    } else if (sortBy === 'oldest') {
      result = [...result].sort(
        (a, b) => new Date(a.submittedAt).getTime() - new Date(b.submittedAt).getTime()
      );
    } else {
      result = [...result].sort((a, b) => b.upvotes.length - a.upvotes.length);
    }

    return result;
  }, [complaints, search, filterStatus, filterBuilding, filterCategory, sortBy]);

  return (
    <div className="max-w-4xl mx-auto space-y-5">
      <div>
        <h2 className="text-xl font-bold text-slate-800">All Complaints</h2>
        <p className="text-sm text-slate-500 mt-0.5">
          {filtered.length} of {complaints.length} complaints shown
        </p>
      </div>

      {/* Filters */}
      <div className="card space-y-3">
        <div className="flex items-center gap-2">
          <Filter size={16} className="text-slate-500" />
          <span className="text-sm font-medium text-slate-700">Filter & Search</span>
        </div>
        <div className="relative">
          <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
          <input
            className="input pl-9"
            placeholder="Search by title, ID, building…"
            value={search}
            onChange={e => setSearch(e.target.value)}
          />
        </div>
        <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
          <select className="select text-sm" value={filterStatus} onChange={e => setFilterStatus(e.target.value as '' | ComplaintStatus)}>
            {STATUS_OPTIONS.map(o => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
          <select className="select text-sm" value={filterType} onChange={e => setFilterType(e.target.value as '' | 'original' | 'followup')}>
            <option value="">All Types</option>
            <option value="original">Original Only</option>
            <option value="followup">Follow-ups Only</option>
          </select>
          <select className="select text-sm" value={filterBuilding} onChange={e => setFilterBuilding(e.target.value)}>
            <option value="">All Buildings</option>
            {BUILDINGS.map(b => <option key={b} value={b}>{b}</option>)}
          </select>
          <select className="select text-sm" value={filterCategory} onChange={e => setFilterCategory(e.target.value as '' | IssueCategory)}>
            <option value="">All Categories</option>
            {ISSUE_CATEGORIES.map(c => <option key={c} value={c}>{c}</option>)}
          </select>
          <select className="select text-sm" value={sortBy} onChange={e => setSortBy(e.target.value as typeof sortBy)}>
            <option value="newest">Newest First</option>
            <option value="oldest">Oldest First</option>
            <option value="upvotes">Most Upvoted</option>
          </select>
        </div>
      </div>

      {/* List */}
      {filtered.length === 0 ? (
        <div className="text-center py-16 text-slate-400">
          <p className="text-4xl mb-3">🔍</p>
          <p className="font-medium text-slate-500">No complaints match your filters.</p>
          <button
            className="mt-3 text-sm text-blue-600 hover:underline"
            onClick={() => { setSearch(''); setFilterStatus(''); setFilterType(''); setFilterBuilding(''); setFilterCategory(''); }}
          >
            Clear filters
          </button>
        </div>
      ) : (
        <div className="space-y-3">
          {filtered.map(c => (
            <ComplaintCard
              key={c.id}
              complaint={c}
              onUpdate={() => setTick(t => t + 1)}
            />
          ))}
        </div>
      )}
    </div>
  );
}
