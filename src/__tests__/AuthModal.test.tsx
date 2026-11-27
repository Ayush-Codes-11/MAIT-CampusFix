import { describe, it, expect, vi, beforeEach } from 'vitest';
import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { AuthModal } from '../components/auth/AuthModal';
import { AuthModalProvider, useAuthModal } from '../context/AuthModalContext';
import * as AuthContextModule from '../context/AuthContext';

// Mock useAuth
const mockSignIn = vi.fn();
const mockSignUp = vi.fn();
const mockConfirmSignUp = vi.fn();
const mockResendSignUpCode = vi.fn();
const mockResetPassword = vi.fn();
const mockConfirmResetPassword = vi.fn();
const mockClearSessionExpired = vi.fn();

function TestWrapper({
  initialMode = 'signin',
  initialEmail = '',
  openInitially = true,
  children,
}: {
  initialMode?: any;
  initialEmail?: string;
  openInitially?: boolean;
  children: React.ReactNode;
}) {
  return (
    <AuthModalProvider>
      <Controller
        initialMode={initialMode}
        initialEmail={initialEmail}
        openInitially={openInitially}
      />
      {children}
    </AuthModalProvider>
  );
}

function Controller({
  initialMode,
  initialEmail,
  openInitially,
}: {
  initialMode: any;
  initialEmail: string;
  openInitially: boolean;
}) {
  const { openAuthModal } = useAuthModal();
  React.useEffect(() => {
    if (openInitially) {
      openAuthModal(initialMode, initialEmail);
    }
  }, [initialMode, initialEmail, openInitially, openAuthModal]);
  return null;
}

describe('AuthModal Component', () => {
  beforeEach(() => {
    vi.clearAllMocks();

    vi.spyOn(AuthContextModule, 'useAuth').mockReturnValue({
      user: null,
      role: null,
      isLoading: false,
      isAuthenticated: false,
      isMockAuth: false,
      configError: null,
      sessionExpired: false,
      signIn: mockSignIn,
      signUp: mockSignUp,
      confirmSignUp: mockConfirmSignUp,
      resendSignUpCode: mockResendSignUpCode,
      signOut: vi.fn(),
      resetPassword: mockResetPassword,
      confirmResetPassword: mockConfirmResetPassword,
      clearSessionExpired: mockClearSessionExpired,
      setMockRole: vi.fn(),
    });
  });

  it('renders sign-in form with email, password, and visibility toggle', async () => {
    render(
      <TestWrapper initialMode="signin">
        <AuthModal />
      </TestWrapper>
    );

    expect(screen.getByText('Sign In to CampusFix')).toBeInTheDocument();
    const emailInput = screen.getByPlaceholderText('e.g. rollnumber@cst.mait.ac.in');
    const passwordInput = screen.getByPlaceholderText('Enter your password');
    expect(emailInput).toBeInTheDocument();
    expect(passwordInput).toHaveAttribute('type', 'password');

    // Toggle password visibility
    const toggleBtn = screen.getByLabelText('Show password');
    fireEvent.click(toggleBtn);
    expect(passwordInput).toHaveAttribute('type', 'text');
  });

  it('submits sign-in credentials and handles success', async () => {
    mockSignIn.mockResolvedValueOnce(undefined);

    render(
      <TestWrapper initialMode="signin">
        <AuthModal />
      </TestWrapper>
    );

    fireEvent.change(screen.getByPlaceholderText('e.g. rollnumber@cst.mait.ac.in'), {
      target: { value: 'student@cst.mait.ac.in' },
    });
    fireEvent.change(screen.getByPlaceholderText('Enter your password'), {
      target: { value: 'Password123!' },
    });

    fireEvent.click(screen.getByRole('button', { name: /sign in/i }));

    await waitFor(() => {
      expect(mockSignIn).toHaveBeenCalledWith('student@cst.mait.ac.in', 'Password123!');
      expect(mockClearSessionExpired).toHaveBeenCalled();
    });
  });

  it('displays error message when sign-in fails', async () => {
    mockSignIn.mockRejectedValueOnce(new Error('Incorrect username or password.'));

    render(
      <TestWrapper initialMode="signin">
        <AuthModal />
      </TestWrapper>
    );

    fireEvent.change(screen.getByPlaceholderText('e.g. rollnumber@cst.mait.ac.in'), {
      target: { value: 'student@cst.mait.ac.in' },
    });
    fireEvent.change(screen.getByPlaceholderText('Enter your password'), {
      target: { value: 'WrongPass' },
    });

    fireEvent.click(screen.getByRole('button', { name: /sign in/i }));

    await waitFor(() => {
      expect(screen.getByText('Incorrect username or password.')).toBeInTheDocument();
    });
  });

  it('transitions to confirm_signup and preserves email when signIn throws UserNotConfirmedException (M-02)', async () => {
    const unconfirmedErr = new Error('User is not confirmed.');
    unconfirmedErr.name = 'UserNotConfirmedException';
    mockSignIn.mockRejectedValueOnce(unconfirmedErr);

    render(
      <TestWrapper initialMode="signin">
        <AuthModal />
      </TestWrapper>
    );

    fireEvent.change(screen.getByPlaceholderText('e.g. rollnumber@cst.mait.ac.in'), {
      target: { value: 'unconfirmed@cst.mait.ac.in' },
    });
    fireEvent.change(screen.getByPlaceholderText('Enter your password'), {
      target: { value: 'Password123!' },
    });

    fireEvent.click(screen.getByRole('button', { name: /sign in/i }));

    await waitFor(() => {
      expect(screen.getByText('Verify Your Email')).toBeInTheDocument();
      expect(
        screen.getByText(/Your account is not verified yet. Please enter the verification code/i)
      ).toBeInTheDocument();
      const emailInput = screen.getByPlaceholderText('rollnumber@cst.mait.ac.in') as HTMLInputElement;
      expect(emailInput.value).toBe('unconfirmed@cst.mait.ac.in');
    });
  });

  it('transitions to confirm_signup and preserves email when signIn returns CONFIRM_SIGN_UP nextStep (M-02)', async () => {
    mockSignIn.mockResolvedValueOnce({
      isSignedIn: false,
      nextStep: { signInStep: 'CONFIRM_SIGN_UP' },
    });

    render(
      <TestWrapper initialMode="signin">
        <AuthModal />
      </TestWrapper>
    );

    fireEvent.change(screen.getByPlaceholderText('e.g. rollnumber@cst.mait.ac.in'), {
      target: { value: 'pendingotp@cst.mait.ac.in' },
    });
    fireEvent.change(screen.getByPlaceholderText('Enter your password'), {
      target: { value: 'Password123!' },
    });

    fireEvent.click(screen.getByRole('button', { name: /sign in/i }));

    await waitFor(() => {
      expect(screen.getByText('Verify Your Email')).toBeInTheDocument();
      expect(
        screen.getByText(/Your account is not verified yet. Please enter the verification code/i)
      ).toBeInTheDocument();
      const emailInput = screen.getByPlaceholderText('rollnumber@cst.mait.ac.in') as HTMLInputElement;
      expect(emailInput.value).toBe('pendingotp@cst.mait.ac.in');
    });
  });

  it('validates password match on sign-up before calling AuthContext', async () => {
    render(
      <TestWrapper initialMode="signup">
        <AuthModal />
      </TestWrapper>
    );

    expect(screen.getByText('Create Student Account')).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText('e.g. Ayush Sharma'), {
      target: { value: 'Ayush Sharma' },
    });
    fireEvent.change(screen.getByPlaceholderText('rollnumber@cst.mait.ac.in'), {
      target: { value: 'ayush@cst.mait.ac.in' },
    });
    fireEvent.change(screen.getByPlaceholderText('At least 8 characters'), {
      target: { value: 'Password123!' },
    });
    fireEvent.change(screen.getByPlaceholderText('Re-enter password'), {
      target: { value: 'DifferentPassword123!' },
    });

    fireEvent.click(screen.getByRole('button', { name: /register account/i }));

    expect(screen.getByText('Passwords do not match.')).toBeInTheDocument();
    expect(mockSignUp).not.toHaveBeenCalled();
  });

  it('submits sign-up and transitions to confirm_signup on unconfirmed response', async () => {
    mockSignUp.mockResolvedValueOnce({ isSignUpComplete: false });

    render(
      <TestWrapper initialMode="signup">
        <AuthModal />
      </TestWrapper>
    );

    fireEvent.change(screen.getByPlaceholderText('e.g. Ayush Sharma'), {
      target: { value: 'Ayush Sharma' },
    });
    fireEvent.change(screen.getByPlaceholderText('rollnumber@cst.mait.ac.in'), {
      target: { value: 'ayush@cst.mait.ac.in' },
    });
    fireEvent.change(screen.getByPlaceholderText('e.g. 00314812725'), {
      target: { value: '00314812725' },
    });
    fireEvent.change(screen.getByPlaceholderText('At least 8 characters'), {
      target: { value: 'Password123!' },
    });
    fireEvent.change(screen.getByPlaceholderText('Re-enter password'), {
      target: { value: 'Password123!' },
    });

    fireEvent.click(screen.getByRole('button', { name: /register account/i }));

    await waitFor(() => {
      expect(mockSignUp).toHaveBeenCalledWith(
        'ayush@cst.mait.ac.in',
        'Password123!',
        'Ayush Sharma',
        '00314812725'
      );
      expect(screen.getByText('Verify Your Email')).toBeInTheDocument();
      // L-01: Success message is preserved across mode transitions
      expect(
        screen.getByText('Account created! Please enter the verification code sent to your email.')
      ).toBeInTheDocument();
    });

    // L-01: Manual switchMode resets messages cleanly without leaking to unrelated views
    fireEvent.click(screen.getByRole('button', { name: /back to sign in/i }));
    expect(screen.getByText('Sign In to CampusFix')).toBeInTheDocument();
    expect(
      screen.queryByText('Account created! Please enter the verification code sent to your email.')
    ).not.toBeInTheDocument();
  });

  it('submits confirm-signup and transitions to sign-in on success', async () => {
    mockConfirmSignUp.mockResolvedValueOnce(undefined);

    render(
      <TestWrapper initialMode="confirm_signup" initialEmail="student@cst.mait.ac.in">
        <AuthModal />
      </TestWrapper>
    );

    expect(screen.getByText('Verify Your Email')).toBeInTheDocument();

    const codeInput = screen.getByPlaceholderText('e.g. 123456');
    fireEvent.change(codeInput, { target: { value: '654321' } });

    fireEvent.click(screen.getByRole('button', { name: /confirm email/i }));

    await waitFor(() => {
      expect(mockConfirmSignUp).toHaveBeenCalledWith('student@cst.mait.ac.in', '654321');
      expect(screen.getByText('Sign In to CampusFix')).toBeInTheDocument();
    });
  });

  it('allows resending verification code in confirm_signup mode', async () => {
    mockResendSignUpCode.mockResolvedValueOnce(undefined);

    render(
      <TestWrapper initialMode="confirm_signup" initialEmail="student@cst.mait.ac.in">
        <AuthModal />
      </TestWrapper>
    );

    const resendBtn = screen.getByRole('button', { name: /resend code/i });
    fireEvent.click(resendBtn);

    await waitFor(() => {
      expect(mockResendSignUpCode).toHaveBeenCalledWith('student@cst.mait.ac.in');
      expect(screen.getByText(/A new verification code has been sent/i)).toBeInTheDocument();
    });
  });

  it('submits forgot-password and transitions to confirm_reset', async () => {
    mockResetPassword.mockResolvedValueOnce(undefined);

    render(
      <TestWrapper initialMode="forgot_password">
        <AuthModal />
      </TestWrapper>
    );

    expect(screen.getByText('Reset Password')).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText('Enter your registered email'), {
      target: { value: 'student@cst.mait.ac.in' },
    });

    fireEvent.click(screen.getByRole('button', { name: /send reset code/i }));

    await waitFor(() => {
      expect(mockResetPassword).toHaveBeenCalledWith('student@cst.mait.ac.in');
      expect(screen.getByText('Set New Password')).toBeInTheDocument();
    });
  });

  it('submits confirm_reset with code and new password', async () => {
    mockConfirmResetPassword.mockResolvedValueOnce(undefined);

    render(
      <TestWrapper initialMode="confirm_reset">
        <AuthModal />
      </TestWrapper>
    );

    fireEvent.change(screen.getByPlaceholderText('Registered email'), {
      target: { value: 'student@cst.mait.ac.in' },
    });
    fireEvent.change(screen.getByPlaceholderText('Enter the code sent to your email'), {
      target: { value: '112233' },
    });
    fireEvent.change(screen.getByPlaceholderText('At least 8 characters'), {
      target: { value: 'BrandNewPass123!' },
    });
    fireEvent.change(screen.getByPlaceholderText('Re-enter new password'), {
      target: { value: 'BrandNewPass123!' },
    });

    fireEvent.click(screen.getByRole('button', { name: /confirm new password/i }));

    await waitFor(() => {
      expect(mockConfirmResetPassword).toHaveBeenCalledWith(
        'student@cst.mait.ac.in',
        '112233',
        'BrandNewPass123!'
      );
    });
  });
});
