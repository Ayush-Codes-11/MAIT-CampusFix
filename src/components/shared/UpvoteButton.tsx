import { ThumbsUp } from 'lucide-react';
import { useState, useEffect } from 'react';
import { toggleUpvote, hasUpvoted } from '../../utils/storage';

interface UpvoteButtonProps {
  complaintId: string;
  count: number;
  onChange?: () => void;
}

export function UpvoteButton({ complaintId, count, onChange }: UpvoteButtonProps) {
  const [voted, setVoted] = useState(() => hasUpvoted(complaintId));
  const [localCount, setLocalCount] = useState(count);

  useEffect(() => {
    setVoted(hasUpvoted(complaintId));
    setLocalCount(count);
  }, [complaintId, count]);

  function handleClick() {
    const result = toggleUpvote(complaintId);
    if (result === 'added') {
      setVoted(true);
      setLocalCount(c => c + 1);
    } else {
      setVoted(false);
      setLocalCount(c => c - 1);
    }
    onChange?.();
  }

  return (
    <button
      onClick={handleClick}
      title={voted ? 'Remove your upvote' : 'Upvote this complaint'}
      className={`flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium border transition-colors duration-150 ${
        voted
          ? 'bg-blue-50 border-blue-200 text-blue-700 hover:bg-blue-100'
          : 'bg-white border-slate-200 text-slate-500 hover:bg-slate-50 hover:text-slate-700'
      }`}
    >
      <ThumbsUp size={13} className={voted ? 'fill-blue-600 text-blue-600' : ''} />
      <span>{localCount}</span>
    </button>
  );
}
