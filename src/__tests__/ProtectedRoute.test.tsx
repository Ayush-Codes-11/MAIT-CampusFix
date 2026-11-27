import { describe, it, expect, vi, beforeEach } from 'vitest';
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { ProtectedRoute } from '../components/auth/ProtectedRoute';
import * as AuthModalModule from '../context/AuthModalContext';
import * as AuthContextModule from '../context/AuthContext';

describe('ProtectedRoute Component', () => {
  let mockOpenAuthModal: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.clearAllMocks();
    mockOpenAuthModal = vi.fn();
    vi.spyOn(AuthModalModule, 'useAuthModal').mockReturnValue({
      isOpen: false,
      mode: 'signin',
      initialEmail: '',
      openAuthModal: mockOpenAuthModal,
      closeAuthModal: vi.fn(),
      setMode: vi.fn(),
    });
  });

  function renderProtected(
    authOverrides: Partial<AuthContextModule.AuthContextType>,
    allowedRoles?: AuthContextModule.UserRole[],
    initialPath = '/protected'
  ) {
    vi.spyOn(AuthContextModule, 'useAuth').mockReturnValue({
      user: null,
      role: null,
      isLoading: false,
      isAuthenticated: false,
      isMockAuth: false,
      configError: null,
      sessionExpired: false,
      signIn: vi.fn(),
      signUp: vi.fn(),
      confirmSignUp: vi.fn(),
      resendSignUpCode: vi.fn(),
      signOut: vi.fn(),
      resetPassword: vi.fn(),
      confirmResetPassword: vi.fn(),
      clearSessionExpired: vi.fn(),
      setMockRole: vi.fn(),
      ...authOverrides,
    });

    return render(
      <MemoryRouter initialEntries={[initialPath]}>
        <Routes>
          <Route
            path="/protected"
            element={
              <ProtectedRoute allowedRoles={allowedRoles}>
                <div data-testid="protected-content">Secret Content</div>
              </ProtectedRoute>
            }
          />
          <Route
            path="/"
            element={
              <ProtectedRoute allowedRoles={['student', 'admin']}>
                <div data-testid="student-dashboard">Student Dashboard Content</div>
              </ProtectedRoute>
            }
          />
          <Route
            path="/admin"
            element={<div data-testid="admin-panel">Admin / Maintenance Panel</div>}
          />
        </Routes>
      </MemoryRouter>
    );
  }

  it('renders loading indicator while session restoration is pending', () => {
    renderProtected({ isLoading: true });

    expect(screen.getByRole('status')).toBeInTheDocument();
    expect(screen.getByText('Verifying authentication session...')).toBeInTheDocument();
    expect(screen.queryByTestId('protected-content')).not.toBeInTheDocument();
  });

  it('renders authentication required card when unauthenticated and triggers openAuthModal', () => {
    renderProtected({ isAuthenticated: false, user: null });

    expect(screen.getByText('Authentication Required')).toBeInTheDocument();
    expect(screen.queryByTestId('protected-content')).not.toBeInTheDocument();

    const signInBtn = screen.getByRole('button', { name: /sign in to campusfix/i });
    expect(signInBtn).toBeInTheDocument();
    fireEvent.click(signInBtn);

    expect(mockOpenAuthModal).toHaveBeenCalledWith('signin');
  });

  it('redirects maintenance users accessing root "/" directly to "/admin"', () => {
    renderProtected(
      {
        isAuthenticated: true,
        user: {
          userId: 'stf-001',
          email: 'staff@mait.ac.in',
          role: 'maintenance',
          isEmailVerified: true,
        },
        role: 'maintenance',
      },
      ['student', 'admin'],
      '/'
    );

    // Maintenance navigating to `/` should be redirected to `/admin`
    expect(screen.getByTestId('admin-panel')).toBeInTheDocument();
    expect(screen.queryByTestId('student-dashboard')).not.toBeInTheDocument();
  });

  it('renders Access Denied 403 when authenticated user lacks allowed role', () => {
    renderProtected(
      {
        isAuthenticated: true,
        user: {
          userId: 'stu-123',
          email: 'student@cst.mait.ac.in',
          role: 'student',
          isEmailVerified: true,
        },
        role: 'student',
      },
      ['admin'],
      '/protected'
    );

    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(screen.getByText('Access Denied')).toBeInTheDocument();
    expect(screen.getByText(/Your verified role/)).toBeInTheDocument();
    expect(screen.queryByTestId('protected-content')).not.toBeInTheDocument();
  });

  it('fails closed and denies access when user role is null or unrecognized', () => {
    renderProtected(
      {
        isAuthenticated: true,
        user: {
          userId: 'malformed-user',
          email: 'bad@cst.mait.ac.in',
          role: null,
          isEmailVerified: true,
        },
        role: null,
      },
      ['student', 'admin'],
      '/protected'
    );

    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(screen.getByText('Access Denied')).toBeInTheDocument();
    expect(screen.queryByTestId('protected-content')).not.toBeInTheDocument();
  });

  it('renders protected children when authenticated user has matching role', () => {
    renderProtected(
      {
        isAuthenticated: true,
        user: {
          userId: 'admin-001',
          email: 'admin@mait.ac.in',
          role: 'admin',
          isEmailVerified: true,
        },
        role: 'admin',
      },
      ['admin'],
      '/protected'
    );

    expect(screen.getByTestId('protected-content')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});
