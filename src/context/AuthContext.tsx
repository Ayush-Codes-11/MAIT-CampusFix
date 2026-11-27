/**
 * Authentication Context & State Management for MAIT CampusFix.
 *
 * Rules:
 * 1. User ID is derived strictly from the verified Access Token's `sub` claim.
 * 2. User Role is derived strictly from the verified Access Token's `cognito:groups` claim:
 *    - 'students' -> 'student'
 *    - 'maintenance' -> 'maintenance'
 *    - 'admins' -> 'admin'
 *    Fails closed (role = null) on missing, unknown, malformed, or multiple recognized groups.
 * 3. Never uses ID token groups or client profile data for authorization.
 * 4. `isEmailVerified` is true only when the actual `email_verified` claim is true.
 * 5. Mock mode is allowed ONLY in non-production when VITE_ENABLE_MOCK_AUTH=true.
 * 6. Hub events from 'aws-amplify/utils' and 'auth:session-expired' window events
 *    are subscribed to manage the session lifecycle.
 */

import React, {
  createContext,
  useContext,
  useEffect,
  useState,
  useCallback,
  useMemo,
  useRef,
} from 'react';
import {
  signIn as amplifySignIn,
  signUp as amplifySignUp,
  confirmSignUp as amplifyConfirmSignUp,
  resendSignUpCode as amplifyResendSignUpCode,
  signOut as amplifySignOut,
  resetPassword as amplifyResetPassword,
  confirmResetPassword as amplifyConfirmResetPassword,
  fetchAuthSession,
} from 'aws-amplify/auth';
import { Hub } from 'aws-amplify/utils';
import { configureAuth } from '../config/auth';

export type UserRole = 'student' | 'maintenance' | 'admin';

export interface AuthUser {
  userId: string;
  email: string;
  name?: string;
  rollNumber?: string;
  role: UserRole | null;
  isEmailVerified: boolean;
}

export interface SignInResult {
  isSignedIn: boolean;
  nextStep?: {
    signInStep: string;
    [key: string]: any;
  };
}

export interface AuthContextType {
  user: AuthUser | null;
  role: UserRole | null;
  isLoading: boolean;
  isAuthenticated: boolean;
  isMockAuth: boolean;
  configError: string | null;
  sessionExpired: boolean;
  signIn: (email: string, password: string) => Promise<SignInResult>;
  signUp: (
    email: string,
    password: string,
    name: string,
    rollNumber?: string
  ) => Promise<{ isSignUpComplete: boolean }>;
  confirmSignUp: (email: string, code: string) => Promise<void>;
  resendSignUpCode: (email: string) => Promise<void>;
  signOut: () => Promise<void>;
  resetPassword: (email: string) => Promise<void>;
  confirmResetPassword: (
    email: string,
    code: string,
    newPassword: string
  ) => Promise<void>;
  clearSessionExpired: () => void;
  setMockRole: (role: UserRole) => void;
}

const MOCK_STORAGE_KEY = 'campusfix_mock_auth_role';

const MOCK_USERS: Record<UserRole, AuthUser> = {
  student: {
    userId: 'mock-stu-001',
    email: 'ayush.00314812725@cst.mait.ac.in',
    name: 'Ayush Sharma',
    rollNumber: '00314812725',
    role: 'student',
    isEmailVerified: true,
  },
  maintenance: {
    userId: 'mock-stf-001',
    email: 'ramesh.kumar@mait.ac.in',
    name: 'Ramesh Kumar (Electrician)',
    role: 'maintenance',
    isEmailVerified: true,
  },
  admin: {
    userId: 'mock-adm-001',
    email: 'estate.officer@mait.ac.in',
    name: 'Estate Officer',
    role: 'admin',
    isEmailVerified: true,
  },
};

/**
 * Strict role derivation from the Access Token's `cognito:groups` claim.
 * Fails closed (returns null) on missing, unknown, malformed, or multiple recognized roles.
 */
export function extractRoleFromGroups(rawGroups: unknown): UserRole | null {
  if (!rawGroups) {
    return null;
  }

  let groupsList: string[] = [];

  if (Array.isArray(rawGroups)) {
    groupsList = rawGroups.filter(
      (g): g is string => typeof g === 'string' && g.trim().length > 0
    );
  } else if (typeof rawGroups === 'string') {
    const trimmed = rawGroups.trim();
    if (!trimmed) {
      return null;
    }
    if (trimmed.startsWith('[') && trimmed.endsWith(']')) {
      try {
        const parsed = JSON.parse(trimmed);
        if (Array.isArray(parsed)) {
          groupsList = parsed.filter(
            (g): g is string => typeof g === 'string' && g.trim().length > 0
          );
        } else {
          return null;
        }
      } catch {
        return null;
      }
    } else {
      groupsList = [trimmed];
    }
  } else {
    return null;
  }

  const recognizedRoles: UserRole[] = [];

  for (const g of groupsList) {
    if (g === 'students') {
      recognizedRoles.push('student');
    } else if (g === 'maintenance') {
      recognizedRoles.push('maintenance');
    } else if (g === 'admins') {
      recognizedRoles.push('admin');
    } else {
      // Unknown group: fail closed per specification
      return null;
    }
  }

  // Strict mutual exclusivity: must have exactly one recognized group
  if (recognizedRoles.length !== 1) {
    return null;
  }

  return recognizedRoles[0];
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [role, setRole] = useState<UserRole | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [sessionExpired, setSessionExpired] = useState<boolean>(false);

  // Sequence counter to prevent stale asynchronous restorations from overwriting newer state (F-03)
  const sessionSeqRef = useRef<number>(0);
  // In-flight restoration promise deduplication ref (F-04)
  const inFlightRestoreRef = useRef<Promise<void> | null>(null);

  // Initialize configuration once
  const config = useMemo(() => configureAuth(), []);

  // Mock role state (for local development only)
  const [mockRole, setMockRoleState] = useState<UserRole>(() => {
    if (typeof window !== 'undefined') {
      const saved = localStorage.getItem(MOCK_STORAGE_KEY);
      if (saved === 'student' || saved === 'maintenance' || saved === 'admin') {
        return saved;
      }
    }
    return 'student';
  });

  const restoreSession = useCallback(async (): Promise<void> => {
    // F-04: Deduplicate concurrent in-flight restoreSession operations
    if (inFlightRestoreRef.current) {
      return inFlightRestoreRef.current;
    }

    const currentSeq = ++sessionSeqRef.current;

    const restorePromise = (async () => {
      setIsLoading(true);

      if (config.isMockAuth) {
        if (currentSeq !== sessionSeqRef.current) return;
        const mockUser = MOCK_USERS[mockRole];
        setUser(mockUser);
        setRole(mockUser.role);
        setIsLoading(false);
        return;
      }

      if (!config.isConfigured) {
        if (currentSeq !== sessionSeqRef.current) return;
        setUser(null);
        setRole(null);
        setIsLoading(false);
        return;
      }

      try {
        const session = await fetchAuthSession();
        // F-03: Discard stale restoration if newer sign-in or sign-out occurred
        if (currentSeq !== sessionSeqRef.current) return;

        const accessToken = session.tokens?.accessToken;
        if (!accessToken) {
          setUser(null);
          setRole(null);
          return;
        }

        const accessPayload = accessToken.payload || {};
        const sub = accessPayload.sub;
        if (typeof sub !== 'string' || !sub.trim()) {
          setUser(null);
          setRole(null);
          return;
        }

        // Derive role exclusively from Access Token groups
        const derivedRole = extractRoleFromGroups(accessPayload['cognito:groups']);

        // Profile attributes from ID token for display only
        const idPayload = session.tokens?.idToken?.payload || {};
        const email =
          (idPayload.email as string) || (accessPayload.username as string) || '';
        const name = (idPayload.name as string) || undefined;
        const rollNumber =
          (idPayload['custom:roll_number'] as string) || undefined;
        const emailVerifiedClaim = idPayload.email_verified;
        const isEmailVerified =
          emailVerifiedClaim === true || emailVerifiedClaim === 'true';

        setUser({
          userId: sub,
          email,
          name,
          rollNumber,
          role: derivedRole,
          isEmailVerified,
        });
        setRole(derivedRole);
      } catch {
        // F-03: Discard stale restoration failure if newer operation intervened
        if (currentSeq !== sessionSeqRef.current) return;
        setUser(null);
        setRole(null);
      } finally {
        // F-03: Only clear loading state if this operation is still current
        if (currentSeq === sessionSeqRef.current) {
          setIsLoading(false);
        }
      }
    })().finally(() => {
      if (inFlightRestoreRef.current === restorePromise) {
        inFlightRestoreRef.current = null;
      }
    });

    inFlightRestoreRef.current = restorePromise;
    return restorePromise;
  }, [config, mockRole]);

  useEffect(() => {
    restoreSession();
  }, [restoreSession]);

  // Subscribe to Hub auth events and session-expired window events
  useEffect(() => {
    const handleSessionExpired = () => {
      sessionSeqRef.current += 1;
      inFlightRestoreRef.current = null;
      setSessionExpired(true);
      setUser(null);
      setRole(null);
    };

    if (typeof window !== 'undefined') {
      window.addEventListener('auth:session-expired', handleSessionExpired);
    }

    let unsubscribeHub: (() => void) | undefined;
    if (config.isConfigured && !config.isMockAuth) {
      unsubscribeHub = Hub.listen('auth', ({ payload }) => {
        switch (payload.event) {
          case 'signedIn':
            restoreSession();
            break;
          case 'signedOut':
            sessionSeqRef.current += 1;
            inFlightRestoreRef.current = null;
            setUser(null);
            setRole(null);
            break;
          case 'tokenRefresh_failure':
            handleSessionExpired();
            break;
        }
      });
    }

    return () => {
      if (typeof window !== 'undefined') {
        window.removeEventListener('auth:session-expired', handleSessionExpired);
      }
      if (unsubscribeHub) {
        unsubscribeHub();
      }
    };
  }, [config, restoreSession]);

  const signIn = useCallback(
    async (email: string, password: string): Promise<SignInResult> => {
      if (config.isMockAuth) {
        // Mock sign in: select student or matching mock user
        const matchedUser =
          Object.values(MOCK_USERS).find((u) => u.email === email) ||
          MOCK_USERS[mockRole];
        setUser(matchedUser);
        setRole(matchedUser.role);
        setSessionExpired(false);
        return {
          isSignedIn: true,
          nextStep: { signInStep: 'DONE' },
        };
      }

      if (!config.isConfigured) {
        throw new Error(
          config.errorMessage ||
            'Authentication is not configured. Missing Cognito credentials.'
        );
      }

      // Invalidate any pre-login session restore operations
      sessionSeqRef.current += 1;
      inFlightRestoreRef.current = null;

      const result = await amplifySignIn({ username: email, password });
      setSessionExpired(false);

      if (result.isSignedIn) {
        await restoreSession();
      } else {
        // Do not treat unconfirmed or incomplete sign-in as authenticated
        setUser(null);
        setRole(null);
      }

      return {
        isSignedIn: Boolean(result.isSignedIn),
        nextStep: result.nextStep,
      };
    },
    [config, mockRole, restoreSession]
  );

  const signUp = useCallback(
    async (
      email: string,
      password: string,
      name: string,
      rollNumber?: string
    ): Promise<{ isSignUpComplete: boolean }> => {
      if (config.isMockAuth) {
        return { isSignUpComplete: false };
      }

      if (!config.isConfigured) {
        throw new Error(
          config.errorMessage ||
            'Authentication is not configured. Missing Cognito credentials.'
        );
      }

      const userAttributes: Record<string, string> = { name };
      if (rollNumber?.trim()) {
        userAttributes['custom:roll_number'] = rollNumber.trim();
      }

      const result = await amplifySignUp({
        username: email,
        password,
        options: {
          userAttributes,
        },
      });

      return { isSignUpComplete: result.isSignUpComplete };
    },
    [config]
  );

  const confirmSignUp = useCallback(
    async (email: string, code: string): Promise<void> => {
      if (config.isMockAuth) {
        return;
      }

      if (!config.isConfigured) {
        throw new Error(
          config.errorMessage ||
            'Authentication is not configured. Missing Cognito credentials.'
        );
      }

      await amplifyConfirmSignUp({
        username: email,
        confirmationCode: code,
      });
    },
    [config]
  );

  const resendSignUpCode = useCallback(
    async (email: string): Promise<void> => {
      if (config.isMockAuth) {
        return;
      }

      if (!config.isConfigured) {
        throw new Error(
          config.errorMessage ||
            'Authentication is not configured. Missing Cognito credentials.'
        );
      }

      await amplifyResendSignUpCode({
        username: email,
      });
    },
    [config]
  );

  const signOut = useCallback(async (): Promise<void> => {
    // Invalidate any active session restore operations
    sessionSeqRef.current += 1;
    inFlightRestoreRef.current = null;

    if (config.isMockAuth) {
      setUser(null);
      setRole(null);
      setSessionExpired(false);
      setIsLoading(false);
      return;
    }

    if (!config.isConfigured) {
      setUser(null);
      setRole(null);
      setSessionExpired(false);
      setIsLoading(false);
      return;
    }

    // F-02: Ensure local authentication state is cleared unconditionally via try-finally,
    // preserving any remote error so callers can catch it.
    try {
      await amplifySignOut({ global: true });
    } finally {
      setUser(null);
      setRole(null);
      setSessionExpired(false);
      setIsLoading(false);
    }
  }, [config]);

  const resetPassword = useCallback(
    async (email: string): Promise<void> => {
      if (config.isMockAuth) {
        return;
      }

      if (!config.isConfigured) {
        throw new Error(
          config.errorMessage ||
            'Authentication is not configured. Missing Cognito credentials.'
        );
      }

      await amplifyResetPassword({ username: email });
    },
    [config]
  );

  const confirmResetPassword = useCallback(
    async (
      email: string,
      code: string,
      newPassword: string
    ): Promise<void> => {
      if (config.isMockAuth) {
        return;
      }

      if (!config.isConfigured) {
        throw new Error(
          config.errorMessage ||
            'Authentication is not configured. Missing Cognito credentials.'
        );
      }

      await amplifyConfirmResetPassword({
        username: email,
        confirmationCode: code,
        newPassword,
      });
    },
    [config]
  );

  const clearSessionExpired = useCallback(() => {
    setSessionExpired(false);
  }, []);

  const setMockRole = useCallback(
    (newRole: UserRole) => {
      if (!config.isMockAuth) {
        console.warn('Role switching is only permitted in Mock Auth mode.');
        return;
      }
      setMockRoleState(newRole);
      if (typeof window !== 'undefined') {
        localStorage.setItem(MOCK_STORAGE_KEY, newRole);
      }
      const updatedMockUser = MOCK_USERS[newRole];
      setUser(updatedMockUser);
      setRole(updatedMockUser.role);
    },
    [config.isMockAuth]
  );

  const value: AuthContextType = {
    user,
    role,
    isLoading,
    isAuthenticated: Boolean(user),
    isMockAuth: config.isMockAuth,
    configError: !config.isConfigured ? config.errorMessage || 'Cognito not configured' : null,
    sessionExpired,
    signIn,
    signUp,
    confirmSignUp,
    resendSignUpCode,
    signOut,
    resetPassword,
    confirmResetPassword,
    clearSessionExpired,
    setMockRole,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextType {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
}
