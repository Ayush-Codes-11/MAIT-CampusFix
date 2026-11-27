import React, { createContext, useContext, useState, useCallback, useMemo } from 'react';

export type AuthModalMode =
  | 'signin'
  | 'signup'
  | 'confirm_signup'
  | 'forgot_password'
  | 'confirm_reset';

export interface AuthModalContextType {
  isOpen: boolean;
  mode: AuthModalMode;
  initialEmail: string;
  openAuthModal: (mode?: AuthModalMode, email?: string) => void;
  closeAuthModal: () => void;
  setMode: (mode: AuthModalMode) => void;
}

const AuthModalContext = createContext<AuthModalContextType | undefined>(undefined);

export function AuthModalProvider({ children }: { children: React.ReactNode }) {
  const [isOpen, setIsOpen] = useState(false);
  const [mode, setModeState] = useState<AuthModalMode>('signin');
  const [initialEmail, setInitialEmail] = useState('');

  const openAuthModal = useCallback(
    (newMode: AuthModalMode = 'signin', email: string = '') => {
      setModeState(newMode);
      setInitialEmail(email);
      setIsOpen(true);
    },
    []
  );

  const closeAuthModal = useCallback(() => {
    setIsOpen(false);
  }, []);

  const setMode = useCallback((newMode: AuthModalMode) => {
    setModeState(newMode);
  }, []);

  const value = useMemo(
    () => ({
      isOpen,
      mode,
      initialEmail,
      openAuthModal,
      closeAuthModal,
      setMode,
    }),
    [isOpen, mode, initialEmail, openAuthModal, closeAuthModal, setMode]
  );

  return (
    <AuthModalContext.Provider value={value}>
      {children}
    </AuthModalContext.Provider>
  );
}

export function useAuthModal(): AuthModalContextType {
  const context = useContext(AuthModalContext);
  if (!context) {
    throw new Error('useAuthModal must be used within an AuthModalProvider');
  }
  return context;
}
