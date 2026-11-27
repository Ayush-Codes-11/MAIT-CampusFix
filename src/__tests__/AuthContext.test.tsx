import { describe, it, expect, vi, beforeEach } from 'vitest';
import React from 'react';
import { render, screen, act, waitFor } from '@testing-library/react';
import {
  AuthProvider,
  useAuth,
  extractRoleFromGroups,
} from '../context/AuthContext';
import * as authConfig from '../config/auth';
import * as amplifyAuth from 'aws-amplify/auth';
import { Hub } from 'aws-amplify/utils';

vi.mock('aws-amplify/auth', () => ({
  signIn: vi.fn(),
  signUp: vi.fn(),
  confirmSignUp: vi.fn(),
  resendSignUpCode: vi.fn(),
  signOut: vi.fn(),
  resetPassword: vi.fn(),
  confirmResetPassword: vi.fn(),
  fetchAuthSession: vi.fn(),
}));

vi.mock('aws-amplify/utils', () => ({
  Hub: {
    listen: vi.fn(() => vi.fn()),
  },
}));

function ConsumerComponent() {
  const auth = useAuth();
  return (
    <div>
      <div data-testid="is-loading">{String(auth.isLoading)}</div>
      <div data-testid="is-authenticated">{String(auth.isAuthenticated)}</div>
      <div data-testid="is-mock">{String(auth.isMockAuth)}</div>
      <div data-testid="config-error">{auth.configError || 'none'}</div>
      <div data-testid="user-id">{auth.user?.userId || 'none'}</div>
      <div data-testid="user-email">{auth.user?.email || 'none'}</div>
      <div data-testid="user-role">{auth.role || 'none'}</div>
      <div data-testid="is-email-verified">{String(auth.user?.isEmailVerified)}</div>
      <div data-testid="session-expired">{String(auth.sessionExpired)}</div>
      <button data-testid="btn-set-admin" onClick={() => auth.setMockRole('admin')}>
        Set Admin
      </button>
      <button
        data-testid="btn-signin"
        onClick={() => auth.signIn('test@cst.mait.ac.in', 'Password123!')}
      >
        Sign In
      </button>
      <button data-testid="btn-signout" onClick={() => auth.signOut()}>
        Sign Out
      </button>
    </div>
  );
}

describe('extractRoleFromGroups', () => {
  it('maps single recognized groups accurately', () => {
    expect(extractRoleFromGroups(['students'])).toBe('student');
    expect(extractRoleFromGroups(['maintenance'])).toBe('maintenance');
    expect(extractRoleFromGroups(['admins'])).toBe('admin');
  });

  it('parses JSON-encoded single group string arrays', () => {
    expect(extractRoleFromGroups('["students"]')).toBe('student');
    expect(extractRoleFromGroups('["maintenance"]')).toBe('maintenance');
    expect(extractRoleFromGroups('["admins"]')).toBe('admin');
  });

  it('parses bare recognized group string', () => {
    expect(extractRoleFromGroups('students')).toBe('student');
    expect(extractRoleFromGroups('maintenance')).toBe('maintenance');
    expect(extractRoleFromGroups('admins')).toBe('admin');
  });

  it('fails closed (returns null) for missing, null, or empty groups', () => {
    expect(extractRoleFromGroups(null)).toBeNull();
    expect(extractRoleFromGroups(undefined)).toBeNull();
    expect(extractRoleFromGroups([])).toBeNull();
    expect(extractRoleFromGroups('   ')).toBeNull();
  });

  it('fails closed (returns null) for conflicting multi-group memberships', () => {
    expect(extractRoleFromGroups(['students', 'admins'])).toBeNull();
    expect(extractRoleFromGroups(['students', 'maintenance'])).toBeNull();
    expect(extractRoleFromGroups(['maintenance', 'admins'])).toBeNull();
    expect(extractRoleFromGroups(['students', 'maintenance', 'admins'])).toBeNull();
  });

  it('fails closed (returns null) for unknown groups and mixed unknown groups', () => {
    expect(extractRoleFromGroups(['alumni'])).toBeNull();
    expect(extractRoleFromGroups(['students', 'alumni'])).toBeNull();
    expect(extractRoleFromGroups('alumni')).toBeNull();
  });

  it('fails closed (returns null) for malformed group representations', () => {
    expect(extractRoleFromGroups('[students')).toBeNull();
    expect(extractRoleFromGroups('students]')).toBeNull();
    expect(extractRoleFromGroups([123])).toBeNull();
    expect(extractRoleFromGroups({})).toBeNull();
  });
});

describe('AuthProvider in Mock Mode', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(authConfig, 'configureAuth').mockReturnValue({
      isConfigured: true,
      isMockAuth: true,
    });
  });

  it('initializes with default mock student user and role', async () => {
    render(
      <AuthProvider>
        <ConsumerComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('is-loading').textContent).toBe('false');
    });

    expect(screen.getByTestId('is-mock').textContent).toBe('true');
    expect(screen.getByTestId('is-authenticated').textContent).toBe('true');
    expect(screen.getByTestId('user-role').textContent).toBe('student');
    expect(screen.getByTestId('user-id').textContent).toBe('mock-stu-001');
    expect(amplifyAuth.fetchAuthSession).not.toHaveBeenCalled();
  });

  it('allows switching mock roles without network calls', async () => {
    render(
      <AuthProvider>
        <ConsumerComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('is-loading').textContent).toBe('false');
    });

    act(() => {
      screen.getByTestId('btn-set-admin').click();
    });

    expect(screen.getByTestId('user-role').textContent).toBe('admin');
    expect(screen.getByTestId('user-id').textContent).toBe('mock-adm-001');
    expect(amplifyAuth.fetchAuthSession).not.toHaveBeenCalled();
  });
});

describe('AuthProvider in Unconfigured State', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(authConfig, 'configureAuth').mockReturnValue({
      isConfigured: false,
      isMockAuth: false,
      errorMessage: 'Cognito credentials missing',
    });
  });

  it('exposes clear config error and leaves user unauthenticated', async () => {
    render(
      <AuthProvider>
        <ConsumerComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('is-loading').textContent).toBe('false');
    });

    expect(screen.getByTestId('is-authenticated').textContent).toBe('false');
    expect(screen.getByTestId('config-error').textContent).toBe('Cognito credentials missing');
    expect(amplifyAuth.fetchAuthSession).not.toHaveBeenCalled();
  });

  it('throws configuration error when calling signIn without Cognito credentials', async () => {
    let capturedAuth: any;
    function TestConsumer() {
      capturedAuth = useAuth();
      return null;
    }

    render(
      <AuthProvider>
        <TestConsumer />
      </AuthProvider>
    );

    await expect(
      capturedAuth.signIn('test@cst.mait.ac.in', 'Password123!')
    ).rejects.toThrow('Cognito credentials missing');
    expect(amplifyAuth.signIn).not.toHaveBeenCalled();
  });
});

describe('AuthProvider in Live Cognito Mode', () => {
  let hubCallback: (data: { payload: { event: string } }) => void;

  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(authConfig, 'configureAuth').mockReturnValue({
      isConfigured: true,
      isMockAuth: false,
    });

    vi.mocked(Hub.listen).mockImplementation((channel, cb: any) => {
      if (channel === 'auth') {
        hubCallback = cb;
      }
      return vi.fn();
    });
  });

  it('restores authenticated session using sub and access token groups', async () => {
    vi.mocked(amplifyAuth.fetchAuthSession).mockResolvedValueOnce({
      tokens: {
        accessToken: {
          payload: {
            sub: 'cognito-sub-uuid-12345',
            'cognito:groups': ['students'],
          },
        } as any,
        idToken: {
          payload: {
            email: 'student001@cst.mait.ac.in',
            name: 'Ayush Sharma',
            'custom:roll_number': '00314812725',
            email_verified: true,
          },
        } as any,
      },
    } as any);

    render(
      <AuthProvider>
        <ConsumerComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('is-loading').textContent).toBe('false');
    });

    expect(screen.getByTestId('is-authenticated').textContent).toBe('true');
    expect(screen.getByTestId('user-id').textContent).toBe('cognito-sub-uuid-12345');
    expect(screen.getByTestId('user-email').textContent).toBe('student001@cst.mait.ac.in');
    expect(screen.getByTestId('user-role').textContent).toBe('student');
    expect(screen.getByTestId('is-email-verified').textContent).toBe('true');
  });

  it('fails closed (sets role to null) if access token has conflicting multi-group roles', async () => {
    vi.mocked(amplifyAuth.fetchAuthSession).mockResolvedValueOnce({
      tokens: {
        accessToken: {
          payload: {
            sub: 'conflicted-sub-uuid',
            'cognito:groups': ['students', 'admins'],
          },
        } as any,
        idToken: {
          payload: {
            email: 'hacker@cst.mait.ac.in',
            email_verified: true,
          },
        } as any,
      },
    } as any);

    render(
      <AuthProvider>
        <ConsumerComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('is-loading').textContent).toBe('false');
    });

    // Authenticated identity preserved, but role fails closed
    expect(screen.getByTestId('is-authenticated').textContent).toBe('true');
    expect(screen.getByTestId('user-role').textContent).toBe('none');
  });

  it('never assumes email_verified is true when claim is absent or false', async () => {
    vi.mocked(amplifyAuth.fetchAuthSession).mockResolvedValueOnce({
      tokens: {
        accessToken: {
          payload: {
            sub: 'unverified-sub-uuid',
            'cognito:groups': ['maintenance'],
          },
        } as any,
        idToken: {
          payload: {
            email: 'staff@mait.ac.in',
            // email_verified omitted!
          },
        } as any,
      },
    } as any);

    render(
      <AuthProvider>
        <ConsumerComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('is-loading').textContent).toBe('false');
    });

    expect(screen.getByTestId('user-role').textContent).toBe('maintenance');
    expect(screen.getByTestId('is-email-verified').textContent).toBe('false');
  });

  it('handles auth:session-expired window event by setting sessionExpired and clearing user', async () => {
    vi.mocked(amplifyAuth.fetchAuthSession).mockResolvedValueOnce({
      tokens: {
        accessToken: {
          payload: {
            sub: 'active-user-sub',
            'cognito:groups': ['students'],
          },
        } as any,
        idToken: {
          payload: { email: 'student@cst.mait.ac.in', email_verified: true },
        } as any,
      },
    } as any);

    render(
      <AuthProvider>
        <ConsumerComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('is-authenticated').textContent).toBe('true');
    });

    act(() => {
      window.dispatchEvent(new CustomEvent('auth:session-expired'));
    });

    expect(screen.getByTestId('session-expired').textContent).toBe('true');
    expect(screen.getByTestId('is-authenticated').textContent).toBe('false');
    expect(screen.getByTestId('user-role').textContent).toBe('none');
  });

  it('responds to Hub tokenRefresh_failure event by marking session expired', async () => {
    vi.mocked(amplifyAuth.fetchAuthSession).mockResolvedValueOnce({
      tokens: {
        accessToken: {
          payload: { sub: 'hub-user-sub', 'cognito:groups': ['students'] },
        } as any,
      },
    } as any);

    render(
      <AuthProvider>
        <ConsumerComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('is-authenticated').textContent).toBe('true');
    });

    act(() => {
      hubCallback({ payload: { event: 'tokenRefresh_failure' } });
    });

    expect(screen.getByTestId('session-expired').textContent).toBe('true');
    expect(screen.getByTestId('is-authenticated').textContent).toBe('false');
  });

  it('executes signOut globally and clears authenticated user', async () => {
    vi.mocked(amplifyAuth.fetchAuthSession).mockResolvedValueOnce({
      tokens: {
        accessToken: {
          payload: { sub: 'active-user-sub', 'cognito:groups': ['students'] },
        } as any,
      },
    } as any);

    render(
      <AuthProvider>
        <ConsumerComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('is-authenticated').textContent).toBe('true');
    });

    await act(async () => {
      screen.getByTestId('btn-signout').click();
    });

    expect(amplifyAuth.signOut).toHaveBeenCalledWith({ global: true });
    expect(screen.getByTestId('is-authenticated').textContent).toBe('false');
    expect(screen.getByTestId('user-role').textContent).toBe('none');
  });

  it('clears local authentication state even when amplifySignOut throws and preserves the remote error (F-02)', async () => {
    vi.mocked(amplifyAuth.fetchAuthSession).mockResolvedValueOnce({
      tokens: {
        accessToken: {
          payload: { sub: 'active-user-sub', 'cognito:groups': ['students'] },
        } as any,
      },
    } as any);

    let hookAuth: any;
    function TestConsumer() {
      hookAuth = useAuth();
      return <ConsumerComponent />;
    }

    render(
      <AuthProvider>
        <TestConsumer />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('is-authenticated').textContent).toBe('true');
    });

    vi.mocked(amplifyAuth.signOut).mockRejectedValueOnce(
      new Error('Network failure during Cognito global sign-out')
    );

    let thrownError: any;
    await act(async () => {
      try {
        await hookAuth.signOut();
      } catch (err) {
        thrownError = err;
      }
    });

    expect(thrownError).toBeDefined();
    expect(thrownError.message).toBe('Network failure during Cognito global sign-out');

    // Guaranteed local clean state despite remote failure
    await waitFor(() => {
      expect(screen.getByTestId('is-authenticated').textContent).toBe('false');
    });
    expect(screen.getByTestId('user-role').textContent).toBe('none');
    expect(hookAuth.user).toBeNull();
    expect(hookAuth.role).toBeNull();
  });

  it('discards stale session restoration that resolves after a newer sign-out has completed (F-03)', async () => {
    let resolveSessionPromise!: (value: any) => void;
    const delayedSessionPromise = new Promise((resolve) => {
      resolveSessionPromise = resolve;
    });

    // Startup restore is delayed
    vi.mocked(amplifyAuth.fetchAuthSession).mockImplementationOnce(
      () => delayedSessionPromise as any
    );

    let hookAuth: any;
    function TestConsumer() {
      hookAuth = useAuth();
      return <ConsumerComponent />;
    }

    render(
      <AuthProvider>
        <TestConsumer />
      </AuthProvider>
    );

    // Sign out happens while startup restore is still in flight
    await act(async () => {
      await hookAuth.signOut();
    });

    expect(screen.getByTestId('is-authenticated').textContent).toBe('false');
    expect(screen.getByTestId('user-role').textContent).toBe('none');

    // The delayed session resolution finally resolves with an authenticated user
    await act(async () => {
      resolveSessionPromise({
        tokens: {
          accessToken: {
            payload: { sub: 'stale-resurrected-sub', 'cognito:groups': ['students'] },
          },
        },
      });
      await delayedSessionPromise;
    });

    // Stale resolution must NOT resurrect the user
    expect(screen.getByTestId('is-authenticated').textContent).toBe('false');
    expect(screen.getByTestId('user-role').textContent).toBe('none');
    expect(hookAuth.user).toBeNull();
    expect(hookAuth.role).toBeNull();
  });

  it('deduplicates simultaneous concurrent restoreSession operations triggered by signIn and Hub (F-04)', async () => {
    let resolveSessionPromise!: (value: any) => void;
    const delayedSessionPromise = new Promise((resolve) => {
      resolveSessionPromise = resolve;
    });

    // Startup restore returns unauthenticated
    vi.mocked(amplifyAuth.fetchAuthSession).mockResolvedValueOnce({
      tokens: undefined,
    } as any);

    let hookAuth: any;
    function TestConsumer() {
      hookAuth = useAuth();
      return <ConsumerComponent />;
    }

    render(
      <AuthProvider>
        <TestConsumer />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('is-loading').textContent).toBe('false');
    });

    // Next call to fetchAuthSession returns the delayed promise
    vi.mocked(amplifyAuth.fetchAuthSession).mockImplementationOnce(
      () => delayedSessionPromise as any
    );

    vi.mocked(amplifyAuth.signIn).mockImplementationOnce(async () => {
      // Simulate Amplify firing Hub signedIn event during signIn execution
      act(() => {
        hubCallback({ payload: { event: 'signedIn' } });
      });
      return {} as any;
    });

    // Execute signIn, which awaits amplifySignIn then calls restoreSession()
    const signInPromise = hookAuth.signIn('student@cst.mait.ac.in', 'Password123!');

    // Resolve the in-flight session
    await act(async () => {
      resolveSessionPromise({
        tokens: {
          accessToken: {
            payload: { sub: 'deduped-sub', 'cognito:groups': ['students'] },
          },
          idToken: {
            payload: { email: 'student@cst.mait.ac.in', email_verified: true },
          },
        },
      });
      await signInPromise;
    });

    expect(screen.getByTestId('is-authenticated').textContent).toBe('true');
    expect(screen.getByTestId('user-id').textContent).toBe('deduped-sub');
    // Total fetchAuthSession calls: 1 on mount + exactly 1 during concurrent sign-in/Hub (deduplicated!)
    expect(amplifyAuth.fetchAuthSession).toHaveBeenCalledTimes(2);
  });

  it('discards stale session restoration for User A when User B signs in before A resolves (regression)', async () => {
    let resolveUserAPromise!: (value: any) => void;
    const delayedUserAPromise = new Promise((resolve) => {
      resolveUserAPromise = resolve;
    });

    // Startup restore for User A is delayed
    vi.mocked(amplifyAuth.fetchAuthSession).mockImplementationOnce(
      () => delayedUserAPromise as any
    );

    let hookAuth: any;
    function TestConsumer() {
      hookAuth = useAuth();
      return <ConsumerComponent />;
    }

    render(
      <AuthProvider>
        <TestConsumer />
      </AuthProvider>
    );

    // User B now signs in while User A's restore is in flight
    vi.mocked(amplifyAuth.signIn).mockResolvedValueOnce({
      isSignedIn: true,
      nextStep: { signInStep: 'DONE' },
    } as any);

    vi.mocked(amplifyAuth.fetchAuthSession).mockResolvedValueOnce({
      tokens: {
        accessToken: {
          payload: { sub: 'user-b-sub', 'cognito:groups': ['students'] },
        },
        idToken: {
          payload: { email: 'userb@cst.mait.ac.in', email_verified: true },
        },
      },
    } as any);

    await act(async () => {
      await hookAuth.signIn('userb@cst.mait.ac.in', 'Password123!');
    });

    expect(screen.getByTestId('is-authenticated').textContent).toBe('true');
    expect(screen.getByTestId('user-id').textContent).toBe('user-b-sub');
    expect(screen.getByTestId('user-email').textContent).toBe('userb@cst.mait.ac.in');

    // Now User A's delayed promise finally resolves
    await act(async () => {
      resolveUserAPromise({
        tokens: {
          accessToken: {
            payload: { sub: 'user-a-sub', 'cognito:groups': ['students'] },
          },
          idToken: {
            payload: { email: 'usera@cst.mait.ac.in', email_verified: true },
          },
        },
      });
      await delayedUserAPromise;
    });

    // CRITICAL: User A's stale resolution must NOT overwrite User B
    expect(screen.getByTestId('is-authenticated').textContent).toBe('true');
    expect(screen.getByTestId('user-id').textContent).toBe('user-b-sub');
    expect(screen.getByTestId('user-email').textContent).toBe('userb@cst.mait.ac.in');
  });

  it('cleans up inFlightRestoreRef when fetchAuthSession throws so subsequent restore can succeed (regression)', async () => {
    // Startup restore fails with a network exception
    vi.mocked(amplifyAuth.fetchAuthSession).mockRejectedValueOnce(
      new Error('Transient network error during session retrieval')
    );

    let hookAuth: any;
    function TestConsumer() {
      hookAuth = useAuth();
      return <ConsumerComponent />;
    }

    render(
      <AuthProvider>
        <TestConsumer />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('is-loading').textContent).toBe('false');
    });
    expect(screen.getByTestId('is-authenticated').textContent).toBe('false');

    // Subsequent signIn / restore should succeed and not be stuck returning the rejected promise
    vi.mocked(amplifyAuth.signIn).mockResolvedValueOnce({
      isSignedIn: true,
      nextStep: { signInStep: 'DONE' },
    } as any);

    vi.mocked(amplifyAuth.fetchAuthSession).mockResolvedValueOnce({
      tokens: {
        accessToken: {
          payload: { sub: 'recovered-sub', 'cognito:groups': ['students'] },
        },
        idToken: {
          payload: { email: 'student@cst.mait.ac.in', email_verified: true },
        },
      },
    } as any);

    await act(async () => {
      await hookAuth.signIn('student@cst.mait.ac.in', 'Password123!');
    });

    expect(screen.getByTestId('is-authenticated').textContent).toBe('true');
    expect(screen.getByTestId('user-id').textContent).toBe('recovered-sub');
  });

  it('does not treat unconfirmed account as authenticated when amplifySignIn returns CONFIRM_SIGN_UP (M-02)', async () => {
    vi.mocked(amplifyAuth.signIn).mockResolvedValueOnce({
      isSignedIn: false,
      nextStep: { signInStep: 'CONFIRM_SIGN_UP' },
    } as any);

    let hookAuth: any;
    function TestConsumer() {
      hookAuth = useAuth();
      return <ConsumerComponent />;
    }

    render(
      <AuthProvider>
        <TestConsumer />
      </AuthProvider>
    );

    let signInResult: any;
    await act(async () => {
      signInResult = await hookAuth.signIn('unconfirmed@cst.mait.ac.in', 'Password123!');
    });

    expect(signInResult.isSignedIn).toBe(false);
    expect(signInResult.nextStep?.signInStep).toBe('CONFIRM_SIGN_UP');
    expect(screen.getByTestId('is-authenticated').textContent).toBe('false');
    expect(screen.getByTestId('user-id').textContent).toBe('none');
  });

  it('delegates signUp with custom rollNumber to Amplify signUp', async () => {
    vi.mocked(amplifyAuth.signUp).mockResolvedValueOnce({
      isSignUpComplete: false,
      userId: 'new-user-id',
      nextStep: { signUpStep: 'CONFIRM_SIGN_UP' },
    } as any);

    let authInstance: any;
    function HookConsumer() {
      authInstance = useAuth();
      return null;
    }

    render(
      <AuthProvider>
        <HookConsumer />
      </AuthProvider>
    );

    let result: any;
    await act(async () => {
      result = await authInstance.signUp(
        'newstudent@cst.mait.ac.in',
        'Password123!',
        'New Student',
        '00314812726'
      );
    });

    expect(result.isSignUpComplete).toBe(false);
    expect(amplifyAuth.signUp).toHaveBeenCalledWith({
      username: 'newstudent@cst.mait.ac.in',
      password: 'Password123!',
      options: {
        userAttributes: {
          name: 'New Student',
          'custom:roll_number': '00314812726',
        },
      },
    });
  });

  it('delegates confirmSignUp and resendSignUpCode to Amplify auth APIs', async () => {
    vi.mocked(amplifyAuth.confirmSignUp).mockResolvedValueOnce({
      isSignUpComplete: true,
      nextStep: { signUpStep: 'DONE' },
    } as any);
    vi.mocked(amplifyAuth.resendSignUpCode).mockResolvedValueOnce({
      destination: 'n***@c***',
      deliveryMedium: 'EMAIL',
    } as any);

    let authInstance: any;
    function HookConsumer() {
      authInstance = useAuth();
      return null;
    }

    render(
      <AuthProvider>
        <HookConsumer />
      </AuthProvider>
    );

    await act(async () => {
      await authInstance.confirmSignUp('newstudent@cst.mait.ac.in', '123456');
      await authInstance.resendSignUpCode('newstudent@cst.mait.ac.in');
    });

    expect(amplifyAuth.confirmSignUp).toHaveBeenCalledWith({
      username: 'newstudent@cst.mait.ac.in',
      confirmationCode: '123456',
    });
    expect(amplifyAuth.resendSignUpCode).toHaveBeenCalledWith({
      username: 'newstudent@cst.mait.ac.in',
    });
  });

  it('delegates resetPassword and confirmResetPassword to Amplify auth APIs', async () => {
    vi.mocked(amplifyAuth.resetPassword).mockResolvedValueOnce({
      isPasswordReset: false,
      nextStep: { resetPasswordStep: 'CONFIRM_RESET_PASSWORD_WITH_CODE' },
    } as any);
    vi.mocked(amplifyAuth.confirmResetPassword).mockResolvedValueOnce(undefined as any);

    let authInstance: any;
    function HookConsumer() {
      authInstance = useAuth();
      return null;
    }

    render(
      <AuthProvider>
        <HookConsumer />
      </AuthProvider>
    );

    await act(async () => {
      await authInstance.resetPassword('student@cst.mait.ac.in');
      await authInstance.confirmResetPassword(
        'student@cst.mait.ac.in',
        '654321',
        'NewStrongPassword123!'
      );
    });

    expect(amplifyAuth.resetPassword).toHaveBeenCalledWith({
      username: 'student@cst.mait.ac.in',
    });
    expect(amplifyAuth.confirmResetPassword).toHaveBeenCalledWith({
      username: 'student@cst.mait.ac.in',
      confirmationCode: '654321',
      newPassword: 'NewStrongPassword123!',
    });
  });
});
