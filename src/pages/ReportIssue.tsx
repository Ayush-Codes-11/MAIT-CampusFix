import { useState, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  BUILDINGS,
  FLOORS,
  ISSUE_CATEGORIES,
  type IssueCategory,
} from '../types';
import { addComplaint, generateComplaintId, getComplaints } from '../utils/storage';
import { useAuth } from '../context/AuthContext';
import { Upload, X, AlertTriangle, CheckCircle2 } from 'lucide-react';

// ─── Similar complaint suggestion (not a block) ───────────────────────────────
function SimilarComplaintsSuggestion({
  building,
  category,
}: {
  building: string;
  category: string;
}) {
  if (!building || !category) return null;
  const existing = getComplaints().filter(
    c =>
      c.building === building &&
      c.category === category &&
      c.status !== 'resolved'
  );
  if (!existing.length) return null;

  return (
    <div className="rounded-xl border border-amber-200 bg-amber-50 p-4">
      <div className="flex gap-2 mb-2">
        <AlertTriangle size={16} className="text-amber-600 flex-shrink-0 mt-0.5" />
        <p className="text-sm font-semibold text-amber-800">
          Similar complaints already exist
        </p>
      </div>
      <p className="text-xs text-amber-700 mb-2">
        These are for reference only — you can still submit your own complaint.
        Consider upvoting the existing one if it matches your issue.
      </p>
      <ul className="space-y-1">
        {existing.slice(0, 3).map(c => (
          <li key={c.id} className="text-xs text-amber-800 bg-amber-100 rounded px-2 py-1">
            <span className="font-mono text-amber-600">{c.id}</span> — {c.title}
          </li>
        ))}
      </ul>
    </div>
  );
}

export default function ReportIssue() {
  const navigate = useNavigate();
  const { user } = useAuth();
  const fileRef = useRef<HTMLInputElement>(null);

  const [form, setForm] = useState({
    building: '',
    floor: '',
    roomNumber: '',
    category: '' as IssueCategory | '',
    title: '',
    description: '',
  });
  const [imageBase64, setImageBase64] = useState<string | undefined>();
  const [imagePreview, setImagePreview] = useState<string | undefined>();
  const [submitted, setSubmitted] = useState(false);
  const [submittedId, setSubmittedId] = useState('');
  const [errors, setErrors] = useState<Partial<Record<keyof typeof form, string>>>({});
  const [authError, setAuthError] = useState<string | null>(null);

  function set(key: keyof typeof form, value: string) {
    setForm(f => ({ ...f, [key]: value }));
    setErrors(e => ({ ...e, [key]: undefined }));
  }

  function handleImage(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    if (file.size > 5 * 1024 * 1024) {
      alert('Image must be under 5 MB');
      return;
    }
    const reader = new FileReader();
    reader.onload = ev => {
      const result = ev.target?.result as string;
      setImageBase64(result);
      setImagePreview(result);
    };
    reader.readAsDataURL(file);
  }

  function validate(): boolean {
    const newErrors: typeof errors = {};
    if (!form.building) newErrors.building = 'Select a building';
    if (!form.floor) newErrors.floor = 'Select a floor';
    if (!form.roomNumber.trim()) newErrors.roomNumber = 'Enter room / area name';
    if (!form.category) newErrors.category = 'Select a category';
    if (!form.title.trim()) newErrors.title = 'Enter a brief title';
    if (form.description.trim().length < 20)
      newErrors.description = 'Please describe the issue in at least 20 characters';
    setErrors(newErrors);
    return Object.keys(newErrors).length === 0;
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setAuthError(null);
    if (!validate()) return;

    if (!user?.userId) {
      setAuthError('You must be signed in with a valid user account to report an issue.');
      return;
    }

    const id = generateComplaintId();
    addComplaint({
      id,
      title: form.title.trim(),
      description: form.description.trim(),
      building: form.building,
      floor: form.floor,
      roomNumber: form.roomNumber.trim(),
      category: form.category as IssueCategory,
      status: 'pending',
      submittedAt: new Date().toISOString(),
      upvotes: [],
      imageBase64,
      submittedBy: user.userId,
      overdueThresholdDays: form.category === 'Fire Safety' ? 3 : 7,
      statusHistory: [], // New complaints always start with no transitions recorded
    });

    setSubmittedId(id);
    setSubmitted(true);
  }

  if (submitted) {
    return (
      <div className="max-w-lg mx-auto mt-16 card text-center space-y-4">
        <div className="w-14 h-14 rounded-full bg-emerald-100 flex items-center justify-center mx-auto">
          <CheckCircle2 size={28} className="text-emerald-600" />
        </div>
        <h2 className="text-lg font-bold text-slate-800">Complaint Submitted!</h2>
        <p className="text-sm text-slate-600">
          Your complaint <span className="font-mono font-semibold text-blue-600">{submittedId}</span> has been recorded.
        </p>
        <div className="rounded-lg bg-amber-50 border border-amber-200 p-3 text-xs text-amber-700 text-left">
          <strong>⚠️ Acknowledgement Notice:</strong> This confirmation only verifies that your
          complaint has been <em>submitted</em> to the CampusFix system. It does not constitute
          official acceptance or guarantee of resolution by MAIT administration.
        </div>
        <div className="flex gap-3 justify-center">
          <button
            onClick={() => {
              setSubmitted(false);
              setForm({ building: '', floor: '', roomNumber: '', category: '', title: '', description: '' });
              setImageBase64(undefined);
              setImagePreview(undefined);
            }}
            className="btn-secondary text-sm"
          >
            Report Another
          </button>
          <button onClick={() => navigate('/complaints')} className="btn-primary text-sm">
            View Complaints
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      <div>
        <h2 className="text-xl font-bold text-slate-800">Report an Issue</h2>
        <p className="text-sm text-slate-500 mt-0.5">
          Describe the problem clearly so it can be addressed quickly.
        </p>
      </div>

      {authError && (
        <div className="p-3 bg-red-50 border border-red-200 rounded-xl text-xs text-red-700 flex items-center gap-2">
          <AlertTriangle size={16} className="text-red-500 flex-shrink-0" />
          <span>{authError}</span>
        </div>
      )}

      <form onSubmit={handleSubmit} className="space-y-5">
        {/* Location */}
        <div className="card space-y-4">
          <h3 className="font-semibold text-slate-700 text-sm">📍 Location</h3>

          <div>
            <label className="block text-xs font-medium text-slate-600 mb-1">
              Building <span className="text-red-500">*</span>
            </label>
            <select
              className="select"
              value={form.building}
              onChange={e => set('building', e.target.value)}
            >
              <option value="">Select building…</option>
              {BUILDINGS.map(b => (
                <option key={b} value={b}>{b}</option>
              ))}
            </select>
            {errors.building && <p className="text-xs text-red-500 mt-1">{errors.building}</p>}
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">
                Floor <span className="text-red-500">*</span>
              </label>
              <select
                className="select"
                value={form.floor}
                onChange={e => set('floor', e.target.value)}
              >
                <option value="">Select floor…</option>
                {FLOORS.map(f => (
                  <option key={f} value={f}>{f}</option>
                ))}
              </select>
              {errors.floor && <p className="text-xs text-red-500 mt-1">{errors.floor}</p>}
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">
                Room / Area <span className="text-red-500">*</span>
              </label>
              <input
                className="input"
                placeholder="e.g. Lab C-204"
                value={form.roomNumber}
                onChange={e => set('roomNumber', e.target.value)}
              />
              {errors.roomNumber && <p className="text-xs text-red-500 mt-1">{errors.roomNumber}</p>}
            </div>
          </div>
        </div>

        {/* Issue details */}
        <div className="card space-y-4">
          <h3 className="font-semibold text-slate-700 text-sm">🔧 Issue Details</h3>

          <div>
            <label className="block text-xs font-medium text-slate-600 mb-1">
              Category <span className="text-red-500">*</span>
            </label>
            <select
              className="select"
              value={form.category}
              onChange={e => set('category', e.target.value)}
            >
              <option value="">Select category…</option>
              {ISSUE_CATEGORIES.map(c => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
            {errors.category && <p className="text-xs text-red-500 mt-1">{errors.category}</p>}
          </div>

          {/* Similar complaints nudge */}
          <SimilarComplaintsSuggestion
            building={form.building}
            category={form.category}
          />

          <div>
            <label className="block text-xs font-medium text-slate-600 mb-1">
              Brief Title <span className="text-red-500">*</span>
            </label>
            <input
              className="input"
              placeholder="e.g. Broken tap in Boys Washroom"
              value={form.title}
              onChange={e => set('title', e.target.value)}
              maxLength={100}
            />
            {errors.title && <p className="text-xs text-red-500 mt-1">{errors.title}</p>}
          </div>

          <div>
            <label className="block text-xs font-medium text-slate-600 mb-1">
              Description <span className="text-red-500">*</span>
            </label>
            <textarea
              className="input resize-none"
              rows={4}
              placeholder="Describe the problem, its impact, and how long it has existed…"
              value={form.description}
              onChange={e => set('description', e.target.value)}
              maxLength={1000}
            />
            <p className="text-xs text-slate-400 text-right mt-0.5">
              {form.description.length}/1000
            </p>
            {errors.description && <p className="text-xs text-red-500">{errors.description}</p>}
          </div>
        </div>

        {/* Image upload */}
        <div className="card">
          <h3 className="font-semibold text-slate-700 text-sm mb-3">📷 Attach Photo (Optional)</h3>
          {imagePreview ? (
            <div className="relative w-full max-w-xs">
              <img src={imagePreview} alt="Preview" className="rounded-lg w-full object-cover max-h-48" />
              <button
                type="button"
                onClick={() => { setImageBase64(undefined); setImagePreview(undefined); }}
                className="absolute top-2 right-2 bg-black/60 hover:bg-black/80 text-white rounded-full p-1"
              >
                <X size={14} />
              </button>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => fileRef.current?.click()}
              className="flex flex-col items-center justify-center w-full border-2 border-dashed border-slate-200 rounded-lg py-8 hover:border-blue-400 hover:bg-blue-50 transition-colors"
            >
              <Upload size={22} className="text-slate-400 mb-2" />
              <p className="text-sm text-slate-500">Click to upload</p>
              <p className="text-xs text-slate-400 mt-0.5">PNG, JPG up to 5 MB</p>
            </button>
          )}
          <input
            ref={fileRef}
            type="file"
            accept="image/*"
            className="hidden"
            onChange={handleImage}
          />
        </div>

        <button type="submit" className="btn-primary w-full py-3 text-sm">
          Submit Complaint
        </button>
      </form>
    </div>
  );
}
