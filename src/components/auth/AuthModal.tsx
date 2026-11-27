import React, { useState, useEffect, useCallback } from 'react';
import { useAuth } from '../../context/AuthContext';
import { useAuthModal, type AuthModalMode } from '../../context/AuthModalContext';
import {
  X,
  Lock,
  Mail,
  User,
  Hash,
  Eye,
  EyeOff,
  AlertCircle,
  CheckCircle2,
  Loader2,
  ArrowRight,
  RotateCcw,
} from 'lucide-react';

export function AuthModal() {
  const {
    isOpen,
    mode,
    initialEmail,
    closeAuthModal,
    setMode,
  } = useAuthModal();

  const {
    signIn,
    signUp,
    confirmSignUp,
    resendSignUpCode,
    resetPassword,
    confirmResetPassword,
    clearSessionExpired,
  } = useAuth();

  // Form Fields
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [name, setName] = useState('');
  const [rollNumber, setRollNumber] = useState('');
  const [code, setCode] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [showConfirmPassword, setShowConfirmPassword] = useState(false);

  // Status & Feedback
  const [isLoading, setIsLoading] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const [resendCooldown, setResendCooldown] = useState(0);

  // Reset form when modal opens
  useEffect(() => {
    if (isOpen) {
      setErrorMessage(null);
      setSuccessMessage(null);
      setPassword('');
      setConfirmPassword('');
      setCode('');
      setShowPassword(false);
      setShowConfirmPassword(false);
      if (initialEmail) {
        setEmail(initialEmail);
      }
    }
  }, [isOpen, initialEmail]);

  // Resend cooldown timer
  useEffect(() => {
    if (resendCooldown > 0) {
      const timer = setTimeout(() => setResendCooldown((c) => c - 1), 1000);
      return () => clearTimeout(timer);
    }
  }, [resendCooldown]);

  const handleClose = useCallback(() => {
    if (isLoading) return;
    setErrorMessage(null);
    setSuccessMessage(null);
    closeAuthModal();
  }, [isLoading, closeAuthModal]);

  // Handle ESC key to close
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && isOpen && !isLoading) {
        handleClose();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [isOpen, isLoading, handleClose]);

  const switchMode = (newMode: AuthModalMode) => {
    setErrorMessage(null);
    setSuccessMessage(null);
    setPassword('');
    setConfirmPassword('');
    setCode('');
    setShowPassword(false);
    setShowConfirmPassword(false);
    setMode(newMode);
  };

  // ── Form Submissions ────────────────────────────────────────────────────────

  const handleSubmitSignIn = async (e: React.FormEvent) => {
    e.preventDefault();
    setErrorMessage(null);
    setSuccessMessage(null);
    setIsLoading(true);

    try {
      const result = await signIn(email.trim(), password);

      if (result && !result.isSignedIn) {
        if (result.nextStep?.signInStep === 'CONFIRM_SIGN_UP') {
          setPassword('');
          setConfirmPassword('');
          setCode('');
          setSuccessMessage(
            'Your account is not verified yet. Please enter the verification code sent to your email.'
          );
          setMode('confirm_signup');
          return;
        }
        setErrorMessage(
          `Additional authentication required (${result.nextStep?.signInStep || 'incomplete'}).`
        );
        return;
      }

      clearSessionExpired();
      closeAuthModal();
    } catch (err: any) {
      if (
        err.name === 'UserNotConfirmedException' ||
        err.__type === 'UserNotConfirmedException' ||
        err.message?.toLowerCase().includes('not confirmed')
      ) {
        setPassword('');
        setConfirmPassword('');
        setCode('');
        setSuccessMessage(
          'Your account is not verified yet. Please enter the verification code sent to your email.'
        );
        setMode('confirm_signup');
        return;
      }
      setErrorMessage(err.message || 'Failed to sign in. Please check your credentials.');
    } finally {
      setIsLoading(false);
    }
  };

  const handleSubmitSignUp = async (e: React.FormEvent) => {
    e.preventDefault();
    setErrorMessage(null);

    if (password !== confirmPassword) {
      setErrorMessage('Passwords do not match.');
      return;
    }

    if (password.length < 8) {
      setErrorMessage('Password must be at least 8 characters long.');
      return;
    }

    setIsLoading(true);

    try {
      const result = await signUp(
        email.trim(),
        password,
        name.trim(),
        rollNumber.trim() || undefined
      );

      if (!result.isSignUpComplete) {
        setSuccessMessage('Account created! Please enter the verification code sent to your email.');
        setMode('confirm_signup');
      } else {
        setSuccessMessage('Account created successfully! Please sign in.');
        setMode('signin');
      }
    } catch (err: any) {
      setErrorMessage(err.message || 'Failed to create account. Please check your details.');
    } finally {
      setIsLoading(false);
    }
  };

  const handleSubmitConfirmSignUp = async (e: React.FormEvent) => {
    e.preventDefault();
    setErrorMessage(null);
    setIsLoading(true);

    try {
      await confirmSignUp(email.trim(), code.trim());
      setSuccessMessage('Email verified successfully! You can now sign in.');
      setMode('signin');
      setPassword('');
      setCode('');
    } catch (err: any) {
      setErrorMessage(err.message || 'Invalid verification code. Please try again.');
    } finally {
      setIsLoading(false);
    }
  };

  const handleResendCode = async () => {
    if (resendCooldown > 0 || isLoading) return;
    setErrorMessage(null);
    setIsLoading(true);

    try {
      await resendSignUpCode(email.trim());
      setSuccessMessage('A new verification code has been sent to your email.');
      setResendCooldown(30);
    } catch (err: any) {
      setErrorMessage(err.message || 'Failed to resend code. Please try again later.');
    } finally {
      setIsLoading(false);
    }
  };

  const handleSubmitForgotPassword = async (e: React.FormEvent) => {
    e.preventDefault();
    setErrorMessage(null);
    setIsLoading(true);

    try {
      await resetPassword(email.trim());
      setSuccessMessage('A password reset code has been sent to your email.');
      setMode('confirm_reset');
    } catch (err: any) {
      setErrorMessage(err.message || 'Failed to initiate password reset.');
    } finally {
      setIsLoading(false);
    }
  };

  const handleSubmitConfirmReset = async (e: React.FormEvent) => {
    e.preventDefault();
    setErrorMessage(null);

    if (password !== confirmPassword) {
      setErrorMessage('Passwords do not match.');
      return;
    }

    if (password.length < 8) {
      setErrorMessage('New password must be at least 8 characters long.');
      return;
    }

    setIsLoading(true);

    try {
      await confirmResetPassword(email.trim(), code.trim(), password);
      setSuccessMessage('Password reset successfully! Please sign in with your new password.');
      setMode('signin');
      setPassword('');
      setConfirmPassword('');
      setCode('');
    } catch (err: any) {
      setErrorMessage(err.message || 'Failed to reset password. Please check your verification code.');
    } finally {
      setIsLoading(false);
    }
  };

  if (!isOpen) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-900/60 backdrop-blur-sm animate-in fade-in duration-150"
      role="dialog"
      aria-modal="true"
      aria-labelledby="auth-modal-title"
      onClick={(e) => {
        if (e.target === e.currentTarget && !isLoading) {
          handleClose();
        }
      }}
    >
      <div className="relative w-full max-w-md bg-white rounded-2xl shadow-2xl border border-slate-100 overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100 bg-slate-50/50">
          <div>
            <h2 id="auth-modal-title" className="text-lg font-bold text-slate-900">
              {mode === 'signin' && 'Sign In to CampusFix'}
              {mode === 'signup' && 'Create Student Account'}
              {mode === 'confirm_signup' && 'Verify Your Email'}
              {mode === 'forgot_password' && 'Reset Password'}
              {mode === 'confirm_reset' && 'Set New Password'}
            </h2>
            <p className="text-xs text-slate-500 mt-0.5">
              {mode === 'signin' && 'Enter your MAIT credentials to access the portal'}
              {mode === 'signup' && 'Register with your verified student email address'}
              {mode === 'confirm_signup' && `Enter the OTP sent to ${email || 'your email'}`}
              {mode === 'forgot_password' && "We'll send a verification code to reset your password"}
              {mode === 'confirm_reset' && 'Enter the code and choose a strong password'}
            </p>
          </div>
          <button
            onClick={handleClose}
            disabled={isLoading}
            className="p-1.5 text-slate-400 hover:text-slate-600 rounded-lg hover:bg-slate-100 transition-colors disabled:opacity-50"
            aria-label="Close dialog"
          >
            <X size={18} />
          </button>
        </div>

        {/* Feedback Banners */}
        {errorMessage && (
          <div className="mx-6 mt-4 p-3 rounded-xl bg-red-50 border border-red-200 flex items-start gap-2.5 text-xs text-red-700 animate-in fade-in">
            <AlertCircle size={16} className="text-red-500 flex-shrink-0 mt-0.5" />
            <div className="flex-1">{errorMessage}</div>
          </div>
        )}

        {successMessage && (
          <div className="mx-6 mt-4 p-3 rounded-xl bg-emerald-50 border border-emerald-200 flex items-start gap-2.5 text-xs text-emerald-700 animate-in fade-in">
            <CheckCircle2 size={16} className="text-emerald-600 flex-shrink-0 mt-0.5" />
            <div className="flex-1">{successMessage}</div>
          </div>
        )}

        {/* Modal Body */}
        <div className="p-6">
          {/* ── Mode 1: SIGN IN ──────────────────────────────────────────────── */}
          {mode === 'signin' && (
            <form onSubmit={handleSubmitSignIn} className="space-y-4">
              <div>
                <label className="block text-xs font-semibold text-slate-700 mb-1">
                  College Email
                </label>
                <div className="relative">
                  <Mail
                    size={16}
                    className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400"
                  />
                  <input
                    type="email"
                    required
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    placeholder="e.g. rollnumber@cst.mait.ac.in"
                    className="w-full pl-9 pr-3 py-2 text-sm rounded-lg border border-slate-200 focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500"
                  />
                </div>
              </div>

              <div>
                <div className="flex items-center justify-between mb-1">
                  <label className="block text-xs font-semibold text-slate-700">
                    Password
                  </label>
                  <button
                    type="button"
                    onClick={() => switchMode('forgot_password')}
                    className="text-xs text-blue-600 hover:underline"
                  >
                    Forgot password?
                  </button>
                </div>
                <div className="relative">
                  <Lock
                    size={16}
                    className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400"
                  />
                  <input
                    type={showPassword ? 'text' : 'password'}
                    required
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    placeholder="Enter your password"
                    className="w-full pl-9 pr-10 py-2 text-sm rounded-lg border border-slate-200 focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500"
                  />
                  <button
                    type="button"
                    onClick={() => setShowPassword((v) => !v)}
                    className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600 p-0.5"
                    aria-label={showPassword ? 'Hide password' : 'Show password'}
                  >
                    {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
                  </button>
                </div>
              </div>

              <button
                type="submit"
                disabled={isLoading}
                className="w-full btn-primary py-2.5 text-sm font-semibold flex items-center justify-center gap-2 mt-2 shadow-sm disabled:opacity-50"
              >
                {isLoading ? (
                  <>
                    <Loader2 size={16} className="animate-spin" />
                    Signing in...
                  </>
                ) : (
                  <>
                    Sign In
                    <ArrowRight size={16} />
                  </>
                )}
              </button>

              <div className="pt-3 text-center text-xs text-slate-500 border-t border-slate-100">
                New student?{' '}
                <button
                  type="button"
                  onClick={() => switchMode('signup')}
                  className="font-semibold text-blue-600 hover:underline"
                >
                  Create an account
                </button>
              </div>
            </form>
          )}

          {/* ── Mode 2: SIGN UP ──────────────────────────────────────────────── */}
          {mode === 'signup' && (
            <form onSubmit={handleSubmitSignUp} className="space-y-3.5">
              <div>
                <label className="block text-xs font-semibold text-slate-700 mb-1">
                  Full Name
                </label>
                <div className="relative">
                  <User
                    size={16}
                    className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400"
                  />
                  <input
                    type="text"
                    required
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="e.g. Ayush Sharma"
                    className="w-full pl-9 pr-3 py-2 text-sm rounded-lg border border-slate-200 focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500"
                  />
                </div>
              </div>

              <div>
                <label className="block text-xs font-semibold text-slate-700 mb-1">
                  College Email Address
                </label>
                <div className="relative">
                  <Mail
                    size={16}
                    className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400"
                  />
                  <input
                    type="email"
                    required
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    placeholder="rollnumber@cst.mait.ac.in"
                    className="w-full pl-9 pr-3 py-2 text-sm rounded-lg border border-slate-200 focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500"
                  />
                </div>
                <p className="text-[11px] text-slate-500 mt-1">
                  Only verified student accounts under <code className="bg-slate-100 px-1 py-0.5 rounded text-blue-700 font-mono">@cst.mait.ac.in</code> are permitted. Staff accounts are provisioned by IT administration.
                </p>
              </div>

              <div>
                <label className="block text-xs font-semibold text-slate-700 mb-1">
                  Roll Number <span className="text-slate-400 font-normal">(optional)</span>
                </label>
                <div className="relative">
                  <Hash
                    size={16}
                    className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400"
                  />
                  <input
                    type="text"
                    value={rollNumber}
                    onChange={(e) => setRollNumber(e.target.value)}
                    placeholder="e.g. 00314812725"
                    className="w-full pl-9 pr-3 py-2 text-sm rounded-lg border border-slate-200 focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500"
                  />
                </div>
              </div>

              <div>
                <label className="block text-xs font-semibold text-slate-700 mb-1">
                  Password
                </label>
                <div className="relative">
                  <Lock
                    size={16}
                    className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400"
                  />
                  <input
                    type={showPassword ? 'text' : 'password'}
                    required
                    minLength={8}
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    placeholder="At least 8 characters"
                    className="w-full pl-9 pr-10 py-2 text-sm rounded-lg border border-slate-200 focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500"
                  />
                  <button
                    type="button"
                    onClick={() => setShowPassword((v) => !v)}
                    className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600 p-0.5"
                    aria-label={showPassword ? 'Hide password' : 'Show password'}
                  >
                    {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
                  </button>
                </div>
              </div>

              <div>
                <label className="block text-xs font-semibold text-slate-700 mb-1">
                  Confirm Password
                </label>
                <div className="relative">
                  <Lock
                    size={16}
                    className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400"
                  />
                  <input
                    type={showConfirmPassword ? 'text' : 'password'}
                    required
                    minLength={8}
                    value={confirmPassword}
                    onChange={(e) => setConfirmPassword(e.target.value)}
                    placeholder="Re-enter password"
                    className="w-full pl-9 pr-10 py-2 text-sm rounded-lg border border-slate-200 focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500"
                  />
                  <button
                    type="button"
                    onClick={() => setShowConfirmPassword((v) => !v)}
                    className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600 p-0.5"
                    aria-label={showConfirmPassword ? 'Hide password' : 'Show password'}
                  >
                    {showConfirmPassword ? <EyeOff size={16} /> : <Eye size={16} />}
                  </button>
                </div>
              </div>

              <button
                type="submit"
                disabled={isLoading}
                className="w-full btn-primary py-2.5 text-sm font-semibold flex items-center justify-center gap-2 mt-2 shadow-sm disabled:opacity-50"
              >
                {isLoading ? (
                  <>
                    <Loader2 size={16} className="animate-spin" />
                    Registering...
                  </>
                ) : (
                  <>
                    Register Account
                    <ArrowRight size={16} />
                  </>
                )}
              </button>

              <div className="pt-2.5 text-center text-xs text-slate-500 border-t border-slate-100">
                Already registered?{' '}
                <button
                  type="button"
                  onClick={() => switchMode('signin')}
                  className="font-semibold text-blue-600 hover:underline"
                >
                  Sign In
                </button>
              </div>
            </form>
          )}

          {/* ── Mode 3: CONFIRM SIGN UP (OTP) ────────────────────────────────── */}
          {mode === 'confirm_signup' && (
            <form onSubmit={handleSubmitConfirmSignUp} className="space-y-4">
              <div>
                <label className="block text-xs font-semibold text-slate-700 mb-1">
                  Email
                </label>
                <div className="relative">
                  <Mail
                    size={16}
                    className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400"
                  />
                  <input
                    type="email"
                    required
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    placeholder="rollnumber@cst.mait.ac.in"
                    className="w-full pl-9 pr-3 py-2 text-sm rounded-lg border border-slate-200 focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 bg-slate-50"
                  />
                </div>
              </div>

              <div>
                <label className="block text-xs font-semibold text-slate-700 mb-1">
                  6-Digit Verification Code
                </label>
                <input
                  type="text"
                  required
                  maxLength={6}
                  value={code}
                  onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))}
                  placeholder="e.g. 123456"
                  className="w-full px-3 py-2 text-center text-lg font-mono tracking-widest rounded-lg border border-slate-200 focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500"
                />
              </div>

              <button
                type="submit"
                disabled={isLoading || code.length !== 6}
                className="w-full btn-primary py-2.5 text-sm font-semibold flex items-center justify-center gap-2 mt-2 shadow-sm disabled:opacity-50"
              >
                {isLoading ? (
                  <>
                    <Loader2 size={16} className="animate-spin" />
                    Verifying...
                  </>
                ) : (
                  <>
                    Confirm Email
                    <CheckCircle2 size={16} />
                  </>
                )}
              </button>

              <div className="flex items-center justify-between pt-3 text-xs text-slate-500 border-t border-slate-100">
                <button
                  type="button"
                  onClick={handleResendCode}
                  disabled={isLoading || resendCooldown > 0}
                  className="font-medium text-blue-600 hover:underline disabled:text-slate-400 flex items-center gap-1"
                >
                  <RotateCcw size={12} />
                  {resendCooldown > 0
                    ? `Resend code (${resendCooldown}s)`
                    : 'Resend code'}
                </button>
                <button
                  type="button"
                  onClick={() => switchMode('signin')}
                  className="font-medium text-slate-600 hover:underline"
                >
                  Back to Sign In
                </button>
              </div>
            </form>
          )}

          {/* ── Mode 4: FORGOT PASSWORD ──────────────────────────────────────── */}
          {mode === 'forgot_password' && (
            <form onSubmit={handleSubmitForgotPassword} className="space-y-4">
              <div>
                <label className="block text-xs font-semibold text-slate-700 mb-1">
                  Registered Email Address
                </label>
                <div className="relative">
                  <Mail
                    size={16}
                    className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400"
                  />
                  <input
                    type="email"
                    required
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    placeholder="Enter your registered email"
                    className="w-full pl-9 pr-3 py-2 text-sm rounded-lg border border-slate-200 focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500"
                  />
                </div>
              </div>

              <button
                type="submit"
                disabled={isLoading}
                className="w-full btn-primary py-2.5 text-sm font-semibold flex items-center justify-center gap-2 mt-2 shadow-sm disabled:opacity-50"
              >
                {isLoading ? (
                  <>
                    <Loader2 size={16} className="animate-spin" />
                    Sending Code...
                  </>
                ) : (
                  <>
                    Send Reset Code
                    <ArrowRight size={16} />
                  </>
                )}
              </button>

              <div className="pt-3 text-center text-xs text-slate-500 border-t border-slate-100">
                Remember your password?{' '}
                <button
                  type="button"
                  onClick={() => switchMode('signin')}
                  className="font-semibold text-blue-600 hover:underline"
                >
                  Sign In
                </button>
              </div>
            </form>
          )}

          {/* ── Mode 5: CONFIRM RESET PASSWORD ───────────────────────────────── */}
          {mode === 'confirm_reset' && (
            <form onSubmit={handleSubmitConfirmReset} className="space-y-3.5">
              <div>
                <label className="block text-xs font-semibold text-slate-700 mb-1">
                  Email
                </label>
                <div className="relative">
                  <Mail
                    size={16}
                    className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400"
                  />
                  <input
                    type="email"
                    required
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    placeholder="Registered email"
                    className="w-full pl-9 pr-3 py-2 text-sm rounded-lg border border-slate-200 focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 bg-slate-50"
                  />
                </div>
              </div>

              <div>
                <label className="block text-xs font-semibold text-slate-700 mb-1">
                  Reset Code
                </label>
                <input
                  type="text"
                  required
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                  placeholder="Enter the code sent to your email"
                  className="w-full px-3 py-2 text-center text-sm font-mono tracking-wider rounded-lg border border-slate-200 focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500"
                />
              </div>

              <div>
                <label className="block text-xs font-semibold text-slate-700 mb-1">
                  New Password
                </label>
                <div className="relative">
                  <Lock
                    size={16}
                    className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400"
                  />
                  <input
                    type={showPassword ? 'text' : 'password'}
                    required
                    minLength={8}
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    placeholder="At least 8 characters"
                    className="w-full pl-9 pr-10 py-2 text-sm rounded-lg border border-slate-200 focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500"
                  />
                  <button
                    type="button"
                    onClick={() => setShowPassword((v) => !v)}
                    className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600 p-0.5"
                    aria-label={showPassword ? 'Hide password' : 'Show password'}
                  >
                    {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
                  </button>
                </div>
              </div>

              <div>
                <label className="block text-xs font-semibold text-slate-700 mb-1">
                  Confirm New Password
                </label>
                <div className="relative">
                  <Lock
                    size={16}
                    className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400"
                  />
                  <input
                    type={showConfirmPassword ? 'text' : 'password'}
                    required
                    minLength={8}
                    value={confirmPassword}
                    onChange={(e) => setConfirmPassword(e.target.value)}
                    placeholder="Re-enter new password"
                    className="w-full pl-9 pr-10 py-2 text-sm rounded-lg border border-slate-200 focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500"
                  />
                  <button
                    type="button"
                    onClick={() => setShowConfirmPassword((v) => !v)}
                    className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600 p-0.5"
                    aria-label={showConfirmPassword ? 'Hide password' : 'Show password'}
                  >
                    {showConfirmPassword ? <EyeOff size={16} /> : <Eye size={16} />}
                  </button>
                </div>
              </div>

              <button
                type="submit"
                disabled={isLoading}
                className="w-full btn-primary py-2.5 text-sm font-semibold flex items-center justify-center gap-2 mt-2 shadow-sm disabled:opacity-50"
              >
                {isLoading ? (
                  <>
                    <Loader2 size={16} className="animate-spin" />
                    Resetting Password...
                  </>
                ) : (
                  <>
                    Confirm New Password
                    <CheckCircle2 size={16} />
                  </>
                )}
              </button>

              <div className="pt-2.5 text-center text-xs text-slate-500 border-t border-slate-100">
                <button
                  type="button"
                  onClick={() => switchMode('signin')}
                  className="font-medium text-blue-600 hover:underline"
                >
                  Back to Sign In
                </button>
              </div>
            </form>
          )}
        </div>
      </div>
    </div>
  );
}
