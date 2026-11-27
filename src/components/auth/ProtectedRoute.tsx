import React from 'react';
import { Navigate, useLocation, Link } from 'react-router-dom';
import { useAuth, type UserRole } from '../../context/AuthContext';
import { useAuthModal } from '../../context/AuthModalContext';
import { ShieldAlert, Loader2, LogIn, ArrowLeft } from 'lucide-react';

interface ProtectedRouteProps {
  allowedRoles?: UserRole[];
  children: React.ReactNode;
}

export function ProtectedRoute({ allowedRoles, children }: ProtectedRouteProps) {
  const { user, role, isLoading, isAuthenticated } = useAuth();
  const { openAuthModal } = useAuthModal();
  const location = useLocation();

  // 1. Initial Authentication Loading State
  if (isLoading) {
    return (
      <div
        className="flex flex-col items-center justify-center min-h-[50vh] space-y-3"
        role="status"
        aria-live="polite"
      >
        <Loader2 className="w-8 h-8 text-blue-600 animate-spin" />
        <p className="text-sm font-medium text-slate-600">
          Verifying authentication session...
        </p>
      </div>
    );
  }

  // 2. Unauthenticated State
  if (!isAuthenticated || !user) {
    return (
      <div className="max-w-md mx-auto my-12 card text-center space-y-4 border-slate-200 shadow-sm">
        <div className="w-12 h-12 bg-blue-50 text-blue-600 rounded-xl flex items-center justify-center mx-auto">
          <LogIn size={24} />
        </div>
        <div>
          <h2 className="text-lg font-bold text-slate-900">
            Authentication Required
          </h2>
          <p className="text-xs text-slate-500 mt-1">
            You must be signed in with your verified MAIT credentials to access this page.
          </p>
        </div>
        <button
          onClick={() => openAuthModal('signin')}
          className="btn-primary w-full py-2 text-sm font-semibold flex items-center justify-center gap-2"
        >
          <LogIn size={16} />
          Sign In to CampusFix
        </button>
      </div>
    );
  }

  // 3. User Decision 1: Special redirect for Maintenance user visiting root `/`
  if (location.pathname === '/' && role === 'maintenance') {
    return <Navigate to="/admin" replace />;
  }

  // 4. Role Authorization Guard (Fails closed on missing or disallowed role)
  if (allowedRoles && (!role || !allowedRoles.includes(role))) {
    return (
      <div
        className="max-w-md mx-auto my-12 card border-amber-200 bg-amber-50/40 text-center space-y-4 shadow-sm"
        role="alert"
      >
        <div className="w-12 h-12 bg-amber-100 text-amber-700 rounded-xl flex items-center justify-center mx-auto">
          <ShieldAlert size={24} />
        </div>
        <div>
          <h2 className="text-lg font-bold text-slate-900">Access Denied</h2>
          <p className="text-xs text-slate-600 mt-1.5 leading-relaxed">
            Your verified role (<span className="font-semibold text-slate-800">{role ?? 'unknown'}</span>) does not have permission to view this section of CampusFix.
          </p>
        </div>
        <div className="flex gap-2 justify-center pt-2">
          {role === 'maintenance' ? (
            <Link
              to="/admin"
              className="btn-primary text-xs py-2 px-4 inline-flex items-center gap-1.5"
            >
              Go to Maintenance Work Panel
            </Link>
          ) : (
            <Link
              to="/"
              className="btn-secondary text-xs py-2 px-4 inline-flex items-center gap-1.5"
            >
              <ArrowLeft size={14} />
              Return to Dashboard
            </Link>
          )}
        </div>
      </div>
    );
  }

  // 5. Authorized
  return <>{children}</>;
}
