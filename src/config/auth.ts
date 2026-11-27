import { Amplify } from 'aws-amplify';
export { Hub } from 'aws-amplify/utils';

export interface AuthConfigResult {
  isConfigured: boolean;
  isMockAuth: boolean;
  errorMessage?: string;
}

/**
 * Configure AWS Amplify authentication with Cognito.
 *
 * Rules:
 * 1. Mock authentication is enabled ONLY when VITE_ENABLE_MOCK_AUTH === 'true'.
 * 2. Mock authentication is STRICTLY FORBIDDEN in production builds (import.meta.env.PROD).
 * 3. If Cognito configuration is missing and mock mode is disabled/forbidden,
 *    an explicit configuration error is returned and logged.
 */
export function configureAuth(): AuthConfigResult {
  const isProd = Boolean(import.meta.env.PROD);
  const mockRequested = import.meta.env.VITE_ENABLE_MOCK_AUTH === 'true';

  if (isProd && mockRequested) {
    const errorMsg = 'FATAL: Mock authentication is strictly forbidden in production builds.';
    console.error(errorMsg);
    throw new Error(errorMsg);
  }

  const userPoolId = import.meta.env.VITE_COGNITO_USER_POOL_ID;
  const userPoolClientId = import.meta.env.VITE_COGNITO_APP_CLIENT_ID;

  // Check if mock mode is legally enabled (non-production and explicitly requested)
  if (!isProd && mockRequested) {
    console.info('Running in Mock Authentication mode (VITE_ENABLE_MOCK_AUTH=true).');
    return { isConfigured: true, isMockAuth: true };
  }

  // Real Cognito Configuration Validation
  if (!userPoolId || !userPoolClientId) {
    const errorMsg =
      'Missing required Cognito configuration: VITE_COGNITO_USER_POOL_ID and ' +
      'VITE_COGNITO_APP_CLIENT_ID must be configured when mock mode is disabled.';
    console.error(errorMsg);
    return {
      isConfigured: false,
      isMockAuth: false,
      errorMessage: errorMsg,
    };
  }

  Amplify.configure({
    Auth: {
      Cognito: {
        userPoolId,
        userPoolClientId,
        signUpVerificationMethod: 'code',
        loginWith: {
          email: true,
        },
      },
    },
  });

  return { isConfigured: true, isMockAuth: false };
}
