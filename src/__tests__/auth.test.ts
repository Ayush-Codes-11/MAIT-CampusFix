import { describe, it, expect, vi, beforeEach } from 'vitest';
import { Amplify } from 'aws-amplify';
import { configureAuth } from '../config/auth';

vi.mock('aws-amplify', () => ({
  Amplify: {
    configure: vi.fn(),
  },
}));

describe('configureAuth', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.unstubAllEnvs();
  });

  it('enables mock mode when VITE_ENABLE_MOCK_AUTH is "true" in non-prod', () => {
    vi.stubEnv('PROD', false);
    vi.stubEnv('VITE_ENABLE_MOCK_AUTH', 'true');

    const result = configureAuth();
    expect(result.isConfigured).toBe(true);
    expect(result.isMockAuth).toBe(true);
    expect(Amplify.configure).not.toHaveBeenCalled();
  });

  it('strictly forbids mock mode in production builds and throws fatal error', () => {
    vi.stubEnv('PROD', true);
    vi.stubEnv('VITE_ENABLE_MOCK_AUTH', 'true');

    expect(() => configureAuth()).toThrow(
      'FATAL: Mock authentication is strictly forbidden in production builds.'
    );
    expect(Amplify.configure).not.toHaveBeenCalled();
  });

  it('returns configuration error when Cognito env vars are missing and mock mode is disabled', () => {
    vi.stubEnv('PROD', false);
    vi.stubEnv('VITE_ENABLE_MOCK_AUTH', 'false');
    vi.stubEnv('VITE_COGNITO_USER_POOL_ID', '');
    vi.stubEnv('VITE_COGNITO_APP_CLIENT_ID', '');

    const result = configureAuth();
    expect(result.isConfigured).toBe(false);
    expect(result.isMockAuth).toBe(false);
    expect(result.errorMessage).toContain('Missing required Cognito configuration');
    expect(Amplify.configure).not.toHaveBeenCalled();
  });

  it('configures Amplify successfully when valid Cognito credentials are provided', () => {
    vi.stubEnv('PROD', false);
    vi.stubEnv('VITE_ENABLE_MOCK_AUTH', 'false');
    vi.stubEnv('VITE_COGNITO_USER_POOL_ID', 'ap-southeast-2_TestPool123');
    vi.stubEnv('VITE_COGNITO_APP_CLIENT_ID', 'app-client-id-abc');

    const result = configureAuth();
    expect(result.isConfigured).toBe(true);
    expect(result.isMockAuth).toBe(false);
    expect(Amplify.configure).toHaveBeenCalledWith({
      Auth: {
        Cognito: {
          userPoolId: 'ap-southeast-2_TestPool123',
          userPoolClientId: 'app-client-id-abc',
          signUpVerificationMethod: 'code',
          loginWith: {
            email: true,
          },
        },
      },
    });
  });
});
