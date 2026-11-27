import { NavLink, useLocation } from 'react-router-dom';
import {
  LayoutDashboard,
  PlusCircle,
  ClipboardList,
  ShieldCheck,
  BarChart3,
  Wrench,
  X,
  Menu,
  LogOut,
  LogIn,
  AlertTriangle,
} from 'lucide-react';
import { useState } from 'react';
import { useAuth, type UserRole } from '../../context/AuthContext';
import { useAuthModal } from '../../context/AuthModalContext';
import { AuthModal } from '../auth/AuthModal';

interface NavItem {
  to: string;
  label: string;
  icon: React.FC<{ size?: number; className?: string }>;
  roles: UserRole[];
}

const ALL_NAV_ITEMS: NavItem[] = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard, roles: ['student', 'admin'] },
  { to: '/report', label: 'Report an Issue', icon: PlusCircle, roles: ['student', 'admin'] },
  { to: '/complaints', label: 'Complaints', icon: ClipboardList, roles: ['student', 'maintenance', 'admin'] },
  { to: '/admin', label: 'Staff / Admin Panel', icon: ShieldCheck, roles: ['maintenance', 'admin'] },
  { to: '/analytics', label: 'Analytics', icon: BarChart3, roles: ['admin'] },
];

function getInitials(name?: string, email?: string): string {
  if (name?.trim()) {
    const parts = name.trim().split(/\s+/);
    if (parts.length >= 2) {
      return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
    }
    return parts[0].slice(0, 2).toUpperCase();
  }
  if (email?.trim()) {
    return email.slice(0, 2).toUpperCase();
  }
  return 'CF';
}

function RoleBadge({ role }: { role: UserRole | null }) {
  if (!role) return null;
  const styles: Record<UserRole, string> = {
    student: 'bg-blue-100 text-blue-700 border-blue-200',
    maintenance: 'bg-amber-100 text-amber-800 border-amber-200',
    admin: 'bg-purple-100 text-purple-700 border-purple-200',
  };
  return (
    <span
      data-testid="user-role-badge"
      className={`text-[10px] uppercase font-bold tracking-wider px-2 py-0.5 rounded-full border ${styles[role]}`}
    >
      {role}
    </span>
  );
}

interface SidebarProps {
  mobileOpen: boolean;
  onClose: () => void;
}

function SidebarContent({ onClose }: { onClose?: () => void }) {
  const { role, isAuthenticated, isMockAuth } = useAuth();
  const { openAuthModal } = useAuthModal();

  const visibleNav = isAuthenticated && role
    ? ALL_NAV_ITEMS.filter((item) => item.roles.includes(role))
    : [];

  return (
    <div className="flex flex-col h-full">
      {/* Logo */}
      <div className="flex items-center gap-2.5 px-5 py-5 border-b border-slate-100">
        <div className="w-8 h-8 bg-blue-600 rounded-lg flex items-center justify-center flex-shrink-0">
          <Wrench size={16} className="text-white" />
        </div>
        <div className="min-w-0">
          <p className="font-semibold text-slate-900 text-sm leading-tight">CampusFix</p>
          <p className="text-xs text-slate-500 leading-tight truncate">MAIT Maintenance Portal</p>
        </div>
        {onClose && (
          <button
            onClick={onClose}
            className="ml-auto p-1 rounded hover:bg-slate-100 lg:hidden"
            aria-label="Close sidebar"
          >
            <X size={18} className="text-slate-500" />
          </button>
        )}
      </div>

      {/* Nav */}
      <nav className="flex-1 overflow-y-auto px-3 py-4 space-y-0.5">
        {isAuthenticated && visibleNav.length > 0 ? (
          visibleNav.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/'}
              onClick={onClose}
              className={({ isActive }) =>
                `flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors duration-100 ${
                  isActive
                    ? 'bg-blue-50 text-blue-700'
                    : 'text-slate-600 hover:bg-slate-100 hover:text-slate-900'
                }`
              }
            >
              <Icon size={18} className="flex-shrink-0" />
              {label}
            </NavLink>
          ))
        ) : (
          <div className="p-3 text-center space-y-2.5 bg-slate-50 rounded-xl border border-slate-100 my-2">
            <p className="text-xs text-slate-600 leading-relaxed">
              Please sign in with your college account to view or file maintenance requests.
            </p>
            <button
              onClick={() => {
                if (onClose) onClose();
                openAuthModal('signin');
              }}
              className="btn-primary w-full py-1.5 text-xs font-semibold flex items-center justify-center gap-1.5"
            >
              <LogIn size={14} />
              Sign In
            </button>
          </div>
        )}
      </nav>

      {/* Footer note */}
      <div className="px-5 py-4 border-t border-slate-100">
        <p className="text-xs text-slate-400 leading-relaxed">
          {isMockAuth ? '🚧 Mock Mode active (local prototype).' : '🔒 Authenticated with AWS Cognito.'}
        </p>
      </div>
    </div>
  );
}

export function Sidebar({ mobileOpen, onClose }: SidebarProps) {
  return (
    <>
      {/* Desktop sidebar */}
      <aside className="hidden lg:flex flex-col w-60 bg-white border-r border-slate-100 h-screen sticky top-0">
        <SidebarContent />
      </aside>

      {/* Mobile overlay */}
      {mobileOpen && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <div
            className="absolute inset-0 bg-black/40 backdrop-blur-sm"
            onClick={onClose}
          />
          <aside className="relative z-50 flex flex-col w-72 max-w-[85vw] bg-white h-full shadow-xl">
            <SidebarContent onClose={onClose} />
          </aside>
        </div>
      )}
    </>
  );
}

export function TopBar({ onMenuClick }: { onMenuClick: () => void }) {
  const location = useLocation();
  const { user, role, isAuthenticated, isMockAuth, setMockRole, signOut } = useAuth();
  const { openAuthModal } = useAuthModal();

  const current = ALL_NAV_ITEMS.find((n) =>
    n.to === '/' ? location.pathname === '/' : location.pathname.startsWith(n.to)
  );

  const displayName = user?.name || user?.email || 'Authenticated User';
  const initials = getInitials(user?.name, user?.email);

  return (
    <header className="sticky top-0 z-30 bg-white border-b border-slate-100 px-4 py-3 flex items-center gap-3">
      <button
        onClick={onMenuClick}
        className="p-2 rounded-lg hover:bg-slate-100 lg:hidden"
        aria-label="Open menu"
      >
        <Menu size={20} className="text-slate-600" />
      </button>
      <h1 className="font-semibold text-slate-800 text-base">
        {current?.label ?? 'CampusFix'}
      </h1>

      <div className="ml-auto flex items-center gap-3">
        {/* Mock Role Switcher (Mock Mode only) */}
        {isMockAuth && (
          <div className="flex items-center gap-1.5 bg-amber-50 border border-amber-200 rounded-lg px-2 py-1 text-xs">
            <span className="text-amber-800 font-medium hidden sm:inline">Mock Role:</span>
            <select
              value={role || 'student'}
              onChange={(e) => setMockRole(e.target.value as UserRole)}
              className="bg-transparent font-semibold text-amber-900 border-none outline-none cursor-pointer text-xs"
              aria-label="Select Mock Role"
            >
              <option value="student">Student</option>
              <option value="maintenance">Maintenance</option>
              <option value="admin">Admin</option>
            </select>
          </div>
        )}

        {isAuthenticated && user ? (
          <div className="flex items-center gap-2.5">
            <RoleBadge role={role} />
            <div className="w-8 h-8 rounded-full bg-blue-600 flex items-center justify-center flex-shrink-0 text-white text-xs font-bold tracking-tight shadow-sm">
              {initials}
            </div>
            <div className="hidden sm:block text-left">
              <p className="text-xs font-semibold text-slate-800 leading-tight max-w-[140px] truncate">
                {displayName}
              </p>
              {user.rollNumber && (
                <p className="text-[10px] text-slate-400 font-mono leading-tight">
                  {user.rollNumber}
                </p>
              )}
            </div>
            <button
              onClick={() => signOut()}
              className="p-1.5 text-slate-500 hover:text-red-600 rounded-lg hover:bg-red-50 transition-colors ml-1"
              title="Sign Out"
              aria-label="Sign Out"
            >
              <LogOut size={16} />
            </button>
          </div>
        ) : (
          <button
            onClick={() => openAuthModal('signin')}
            className="btn-primary py-1.5 px-3 text-xs font-semibold flex items-center gap-1.5"
          >
            <LogIn size={14} />
            Sign In
          </button>
        )}
      </div>
    </header>
  );
}

export function AppLayout({ children }: { children: React.ReactNode }) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const { sessionExpired, clearSessionExpired } = useAuth();
  const { openAuthModal } = useAuthModal();

  return (
    <div className="flex min-h-screen bg-slate-50/50">
      <Sidebar mobileOpen={mobileOpen} onClose={() => setMobileOpen(false)} />
      <div className="flex-1 flex flex-col min-w-0">
        <TopBar onMenuClick={() => setMobileOpen(true)} />

        {/* Canonical Session Expired Banner */}
        {sessionExpired && (
          <div
            className="bg-amber-500 text-white px-4 py-2.5 text-xs font-medium flex items-center justify-between shadow-sm"
            role="alert"
          >
            <div className="flex items-center gap-2">
              <AlertTriangle size={16} className="flex-shrink-0" />
              <span>Your session has expired. Please sign in again to continue accessing CampusFix.</span>
            </div>
            <button
              onClick={() => {
                clearSessionExpired();
                openAuthModal('signin');
              }}
              className="bg-white text-amber-900 px-3 py-1 rounded text-xs font-bold hover:bg-amber-50 transition-colors flex-shrink-0 ml-3"
            >
              Sign In Again
            </button>
          </div>
        )}

        <main className="flex-1 p-4 md:p-6 overflow-auto">
          {children}
        </main>
      </div>

      {/* Single canonical AuthModal instance */}
      <AuthModal />
    </div>
  );
}
