/**
 * Network service layer for MAIT CampusFix.
 *
 * Provides:
 * 1. `authenticatedFetch`: Dispatches requests to the API Gateway backend with
 *    automatic Bearer token injection, bounded single-retry on 401 after forced
 *    token refresh, and strict 403 permission preservation.
 * 2. `uploadToPresignedS3`: Dedicated helper for uploading images directly to
 *    S3 presigned POST targets without attaching the API Bearer Authorization header.
 */

import { fetchAuthSession } from 'aws-amplify/auth';
import { configureAuth } from '../config/auth';

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

export class ForbiddenError extends ApiError {
  constructor(message: string = 'Access denied: insufficient permissions.') {
    super(403, message);
    this.name = 'ForbiddenError';
  }
}

/**
 * Check if a value is a plain JavaScript object suitable for JSON serialization.
 * Excludes FormData, Blob, URLSearchParams, ArrayBuffer, TypedArrays, ReadableStream.
 */
export function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (value === null || typeof value !== 'object') {
    return false;
  }
  if (
    (typeof FormData !== 'undefined' && value instanceof FormData) ||
    (typeof Blob !== 'undefined' && value instanceof Blob) ||
    (typeof URLSearchParams !== 'undefined' && value instanceof URLSearchParams) ||
    (typeof ArrayBuffer !== 'undefined' && value instanceof ArrayBuffer) ||
    (typeof ArrayBuffer !== 'undefined' && ArrayBuffer.isView(value)) ||
    (typeof ReadableStream !== 'undefined' && value instanceof ReadableStream)
  ) {
    return false;
  }
  const proto = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}

/**
 * Dispatches an authenticated HTTP request to the MAIT CampusFix backend API Gateway.
 *
 * Rules:
 * - Rejects calls with ApiError(0) if Amplify is unconfigured or mock mode is active (preventing fake or unconfigured requests to the real backend).
 * - Requires a valid Access Token before dispatching; throws ApiError(401) if session retrieval fails or token is missing.
 * - Automatically injects current Access Token as Bearer token into Authorization header.
 * - Safely serializes plain objects as JSON, setting Content-Type: application/json.
 * - Preserves FormData, Blob, URLSearchParams, and other valid BodyInit values without alteration.
 * - Implements a deterministic single-retry on 401 via explicit forced refresh.
 * - A failed refresh or a second 401 dispatches 'auth:session-expired' and throws ApiError(401).
 * - A 403 Forbidden throws ForbiddenError without logging the user out.
 * - Network errors and 5xx errors propagate normally.
 *
 * @param endpoint Relative API route (e.g. '/complaints').
 * @param options RequestInit options.
 * @param isRetry Whether this invocation is a retry.
 * @param forcedToken Refreshed token to use on retry.
 */
export async function authenticatedFetch(
  endpoint: string,
  options: RequestInit = {},
  isRetry: boolean = false,
  forcedToken?: string
): Promise<Response> {
  // 1. Pre-flight check: Verify configuration and mode safety
  const authConfig = configureAuth();
  if (authConfig.isMockAuth) {
    throw new ApiError(
      0,
      'Mock authentication mode is active. Real backend requests are disabled in mock mode.'
    );
  }
  if (!authConfig.isConfigured) {
    throw new ApiError(
      0,
      authConfig.errorMessage ||
        'Authentication is not configured: missing Cognito credentials.'
    );
  }

  const headers = new Headers(options.headers || {});

  // 2. Resolve Access Token (prefer explicit forcedToken on retry)
  let accessToken: string | undefined = forcedToken;
  if (!accessToken) {
    try {
      const session = await fetchAuthSession();
      accessToken = session.tokens?.accessToken?.toString();
    } catch {
      throw new ApiError(
        401,
        'User is not authenticated or session retrieval failed.'
      );
    }
  }

  if (!accessToken) {
    throw new ApiError(401, 'No valid access token available.');
  }

  if (!headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${accessToken}`);
  }

  // 3. Safe Body Serialization
  let body = options.body;
  if (isPlainObject(body)) {
    body = JSON.stringify(body);
    if (!headers.has('Content-Type')) {
      headers.set('Content-Type', 'application/json');
    }
  }

  const rawBaseUrl = import.meta.env.VITE_API_BASE_URL || '/v1';
  const baseUrl = rawBaseUrl.endsWith('/') ? rawBaseUrl.slice(0, -1) : rawBaseUrl;
  const normalizedEndpoint = endpoint.startsWith('/') ? endpoint : `/${endpoint}`;
  const url = `${baseUrl}${normalizedEndpoint}`;

  // Network errors from fetch propagate normally here
  const response = await fetch(url, { ...options, headers, body });

  // 4. Handle 401 Unauthorized
  if (response.status === 401) {
    if (isRetry) {
      // Second 401: the retried request returned 401
      if (typeof window !== 'undefined') {
        window.dispatchEvent(new CustomEvent('auth:session-expired'));
      }
      throw new ApiError(401, 'Session expired or invalid credentials. Please sign in again.');
    }

    // First 401: attempt forced token refresh
    let refreshedToken: string | undefined;
    try {
      const refreshedSession = await fetchAuthSession({ forceRefresh: true });
      refreshedToken = refreshedSession.tokens?.accessToken?.toString();
      if (!refreshedToken) {
        throw new Error('Forced refresh returned no access token.');
      }
    } catch {
      // Refresh failure expires the session
      if (typeof window !== 'undefined') {
        window.dispatchEvent(new CustomEvent('auth:session-expired'));
      }
      throw new ApiError(401, 'Session expired. Please sign in again.');
    }

    // Refresh succeeded: retry at most once using the refreshed token
    const retryHeaders = new Headers(options.headers || {});
    retryHeaders.set('Authorization', `Bearer ${refreshedToken}`);
    const retryOptions: RequestInit = {
      ...options,
      headers: retryHeaders,
    };

    return authenticatedFetch(endpoint, retryOptions, true, refreshedToken);
  }

  // 5. Handle 403 Forbidden: Permission Error (No retry, No logout, No session-expired)
  if (response.status === 403) {
    const errData = await response.json().catch(() => ({}));
    throw new ForbiddenError(errData.message || 'Access denied: insufficient permissions.');
  }

  return response;
}

/**
 * Dedicated helper for uploading image files directly to Amazon S3 presigned POST targets.
 * Strictly avoids attaching the API Gateway Authorization Bearer header.
 *
 * Rules:
 * - Appends all policy fields to FormData before the file field.
 * - Places the file object as the mandatory final form field.
 * - Never attaches Authorization header.
 * - Leaves Content-Type unset so the browser sets the multipart/form-data boundary.
 */
export async function uploadToPresignedS3(
  postUrl: string,
  fields: Record<string, string>,
  file: File
): Promise<Response> {
  const formData = new FormData();

  // Mandatory S3 rule: append all policy fields before the file field
  for (const [key, value] of Object.entries(fields)) {
    formData.append(key, value);
  }
  // The file MUST be the final field in the form data
  formData.append('file', file);

  // Dispatch fetch without custom Authorization or Content-Type headers
  const response = await fetch(postUrl, {
    method: 'POST',
    body: formData,
  });

  if (!response.ok) {
    throw new Error(`S3 direct upload failed with status ${response.status} ${response.statusText}`);
  }

  return response;
}
