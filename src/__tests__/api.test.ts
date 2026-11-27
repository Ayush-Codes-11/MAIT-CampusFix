import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fetchAuthSession } from 'aws-amplify/auth';
import { configureAuth } from '../config/auth';
import {
  authenticatedFetch,
  uploadToPresignedS3,
  isPlainObject,
  ApiError,
  ForbiddenError,
} from '../services/api';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(),
}));

vi.mock('../config/auth', () => ({
  configureAuth: vi.fn(),
}));

describe('isPlainObject helper', () => {
  it('returns true for plain object literals and Object.create(null)', () => {
    expect(isPlainObject({})).toBe(true);
    expect(isPlainObject({ key: 'value', nested: { a: 1 } })).toBe(true);
    expect(isPlainObject(Object.create(null))).toBe(true);
  });

  it('returns false for built-in body types, primitives, and null', () => {
    expect(isPlainObject(null)).toBe(false);
    expect(isPlainObject(undefined)).toBe(false);
    expect(isPlainObject('string')).toBe(false);
    expect(isPlainObject(12345)).toBe(false);
    expect(isPlainObject([1, 2, 3])).toBe(false);
    expect(isPlainObject(new FormData())).toBe(false);
    expect(isPlainObject(new Blob(['hello']))).toBe(false);
    expect(isPlainObject(new URLSearchParams('a=1'))).toBe(false);
    expect(isPlainObject(new ArrayBuffer(8))).toBe(false);
    expect(isPlainObject(new Uint8Array(8))).toBe(false);
  });
});

describe('authenticatedFetch', () => {
  let mockFetch: ReturnType<typeof vi.fn>;
  let sessionExpiredDispatched: boolean;

  beforeEach(() => {
    vi.clearAllMocks();
    sessionExpiredDispatched = false;

    window.addEventListener('auth:session-expired', () => {
      sessionExpiredDispatched = true;
    });

    // Default mock: valid Amplify configuration
    vi.mocked(configureAuth).mockReturnValue({
      isConfigured: true,
      isMockAuth: false,
    });

    // Default mock: valid session with access token
    vi.mocked(fetchAuthSession).mockResolvedValue({
      tokens: {
        accessToken: {
          toString: () => 'valid-access-token-123',
        } as any,
      },
    } as any);

    mockFetch = vi.fn();
    global.fetch = mockFetch;
  });

  it('rejects with ApiError(0) without dispatching network request when Amplify is unconfigured', async () => {
    vi.mocked(configureAuth).mockReturnValueOnce({
      isConfigured: false,
      isMockAuth: false,
      errorMessage: 'Missing required Cognito configuration',
    });

    try {
      await authenticatedFetch('/complaints');
      expect.unreachable('Should have thrown ApiError');
    } catch (err: any) {
      expect(err).toBeInstanceOf(ApiError);
      expect(err.status).toBe(0);
      expect(err.message).toBe('Missing required Cognito configuration');
    }

    expect(mockFetch).not.toHaveBeenCalled();
    expect(fetchAuthSession).not.toHaveBeenCalled();
  });

  it('rejects with ApiError(0) without dispatching network request when mock auth mode is enabled', async () => {
    vi.mocked(configureAuth).mockReturnValueOnce({
      isConfigured: true,
      isMockAuth: true,
    });

    try {
      await authenticatedFetch('/complaints');
      expect.unreachable('Should have thrown ApiError');
    } catch (err: any) {
      expect(err).toBeInstanceOf(ApiError);
      expect(err.status).toBe(0);
      expect(err.message).toBe(
        'Mock authentication mode is active. Real backend requests are disabled in mock mode.'
      );
    }

    expect(mockFetch).not.toHaveBeenCalled();
    expect(fetchAuthSession).not.toHaveBeenCalled();
  });

  it('rejects with ApiError(401) without dispatching network request when fetchAuthSession throws', async () => {
    vi.mocked(fetchAuthSession).mockRejectedValueOnce(
      new Error('No current user logged in')
    );

    try {
      await authenticatedFetch('/complaints');
      expect.unreachable('Should have thrown ApiError');
    } catch (err: any) {
      expect(err).toBeInstanceOf(ApiError);
      expect(err.status).toBe(401);
      expect(err.message).toBe(
        'User is not authenticated or session retrieval failed.'
      );
    }

    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('rejects with ApiError(401) without dispatching network request when session tokens or access token are missing', async () => {
    vi.mocked(fetchAuthSession).mockResolvedValueOnce({
      tokens: undefined,
    } as any);

    try {
      await authenticatedFetch('/complaints');
      expect.unreachable('Should have thrown ApiError');
    } catch (err: any) {
      expect(err).toBeInstanceOf(ApiError);
      expect(err.status).toBe(401);
      expect(err.message).toBe('No valid access token available.');
    }

    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('injects Bearer access token into Authorization header and normalizes endpoint URL', async () => {
    mockFetch.mockResolvedValueOnce(new Response(JSON.stringify({ success: true }), { status: 200 }));

    // Test with relative endpoint lacking leading slash
    const response = await authenticatedFetch('complaints');
    expect(response.status).toBe(200);

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = mockFetch.mock.calls[0];
    expect(calledUrl).toBe('/v1/complaints');
    expect(calledOptions.headers.get('Authorization')).toBe('Bearer valid-access-token-123');
  });

  it('serializes plain object body as JSON and sets Content-Type header', async () => {
    mockFetch.mockResolvedValueOnce(new Response(JSON.stringify({ id: 'CF-1' }), { status: 201 }));

    const payload = { title: 'Broken tap', category: 'Plumbing' };
    await authenticatedFetch('/complaints', {
      method: 'POST',
      body: payload as any,
    });

    const [, calledOptions] = mockFetch.mock.calls[0];
    expect(calledOptions.body).toBe(JSON.stringify(payload));
    expect(calledOptions.headers.get('Content-Type')).toBe('application/json');
  });

  it('preserves FormData body without converting to JSON or overriding Content-Type', async () => {
    mockFetch.mockResolvedValueOnce(new Response(JSON.stringify({ ok: true }), { status: 200 }));

    const formData = new FormData();
    formData.append('title', 'Door hinge');

    await authenticatedFetch('/complaints/upload', {
      method: 'POST',
      body: formData,
    });

    const [, calledOptions] = mockFetch.mock.calls[0];
    expect(calledOptions.body).toBe(formData);
    expect(calledOptions.headers.get('Content-Type')).toBeNull();
  });

  it('recovers on 401: executes forced refresh and retries exactly once with refreshed token', async () => {
    // 1st request returns 401
    mockFetch.mockResolvedValueOnce(new Response('Unauthorized', { status: 401 }));
    // Retried request returns 200 OK
    mockFetch.mockResolvedValueOnce(new Response(JSON.stringify({ recovered: true }), { status: 200 }));

    // Mock forced refresh
    vi.mocked(fetchAuthSession).mockImplementation(async (options?: { forceRefresh?: boolean }) => {
      if (options?.forceRefresh) {
        return {
          tokens: {
            accessToken: {
              toString: () => 'refreshed-token-999',
            } as any,
          },
        } as any;
      }
      return {
        tokens: {
          accessToken: {
            toString: () => 'initial-token-111',
          } as any,
        },
      } as any;
    });

    const res = await authenticatedFetch('/protected-endpoint');
    expect(res.status).toBe(200);

    // Verify exactly 2 fetch calls
    expect(mockFetch).toHaveBeenCalledTimes(2);

    // First call had initial token
    const [, firstOptions] = mockFetch.mock.calls[0];
    expect(firstOptions.headers.get('Authorization')).toBe('Bearer initial-token-111');

    // Second call used refreshed token
    const [, retryOptions] = mockFetch.mock.calls[1];
    expect(retryOptions.headers.get('Authorization')).toBe('Bearer refreshed-token-999');

    // Session-expired was NOT dispatched
    expect(sessionExpiredDispatched).toBe(false);
  });

  it('expires session when token refresh fails on initial 401', async () => {
    // First request returns 401
    mockFetch.mockResolvedValueOnce(new Response('Unauthorized', { status: 401 }));

    // Forced refresh throws (e.g. Refresh Token expired or revoked)
    vi.mocked(fetchAuthSession).mockImplementation(async (options?: { forceRefresh?: boolean }) => {
      if (options?.forceRefresh) {
        throw new Error('Refresh token has expired');
      }
      return {
        tokens: {
          accessToken: {
            toString: () => 'stale-token',
          } as any,
        },
      } as any;
    });

    await expect(authenticatedFetch('/protected-endpoint')).rejects.toThrow(ApiError);
    // Verified: Exactly 1 fetch call, no retry attempted
    expect(mockFetch).toHaveBeenCalledTimes(1);
    // Verified: Session-expired event dispatched
    expect(sessionExpiredDispatched).toBe(true);
  });

  it('expires session when retried request also returns 401 (bounded at exactly 1 retry)', async () => {
    // 1st request returns 401
    mockFetch.mockResolvedValueOnce(new Response('Unauthorized', { status: 401 }));
    // 2nd request still returns 401
    mockFetch.mockResolvedValueOnce(new Response('Unauthorized', { status: 401 }));

    vi.mocked(fetchAuthSession).mockImplementation(async () => ({
      tokens: {
        accessToken: {
          toString: () => 'new-token',
        } as any,
      },
    } as any));

    await expect(authenticatedFetch('/protected-endpoint')).rejects.toThrow(ApiError);
    // Verified: Exactly 2 fetch calls, no infinite loop
    expect(mockFetch).toHaveBeenCalledTimes(2);
    // Verified: Session-expired event dispatched
    expect(sessionExpiredDispatched).toBe(true);
  });

  it('throws ForbiddenError on 403 without retrying or expiring the session', async () => {
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ message: 'Staff role required' }), { status: 403 })
    );

    await expect(authenticatedFetch('/admin/reports')).rejects.toThrow(ForbiddenError);

    // Verified: Exactly 1 call, no refresh attempted
    expect(mockFetch).toHaveBeenCalledTimes(1);
    expect(vi.mocked(fetchAuthSession)).toHaveBeenCalledTimes(1); // Only the initial token lookup
    // Verified: Session was NOT expired, user is NOT logged out
    expect(sessionExpiredDispatched).toBe(false);
  });

  it('propagates 403 returned on retry as ForbiddenError without expiring session', async () => {
    // 1st request returns 401
    mockFetch.mockResolvedValueOnce(new Response('Unauthorized', { status: 401 }));
    // Retried request returns 403 Forbidden
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ message: 'Role revoked' }), { status: 403 })
    );

    vi.mocked(fetchAuthSession).mockImplementation(async () => ({
      tokens: {
        accessToken: {
          toString: () => 'refreshed-token',
        } as any,
      },
    } as any));

    await expect(authenticatedFetch('/admin/reports')).rejects.toThrow(ForbiddenError);
    expect(mockFetch).toHaveBeenCalledTimes(2);
    // 403 must not log user out
    expect(sessionExpiredDispatched).toBe(false);
  });

  it('propagates 5xx errors normally without expiring session', async () => {
    mockFetch.mockResolvedValueOnce(new Response('Internal Server Error', { status: 500 }));

    const res = await authenticatedFetch('/complaints');
    expect(res.status).toBe(500);
    expect(mockFetch).toHaveBeenCalledTimes(1);
    expect(sessionExpiredDispatched).toBe(false);
  });

  it('propagates network errors normally without expiring session', async () => {
    mockFetch.mockRejectedValueOnce(new TypeError('Failed to fetch'));

    await expect(authenticatedFetch('/complaints')).rejects.toThrow(TypeError);
    expect(sessionExpiredDispatched).toBe(false);
  });
});

describe('uploadToPresignedS3', () => {
  let mockFetch: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.clearAllMocks();
    mockFetch = vi.fn();
    global.fetch = mockFetch;
  });

  it('uploads to presigned S3 target without attaching Authorization header', async () => {
    mockFetch.mockResolvedValueOnce(new Response(null, { status: 204 }));

    const file = new File(['dummy-image-bytes'], 'complaint.jpg', { type: 'image/jpeg' });
    const fields = {
      key: 'uploads/complaint-123.jpg',
      bucket: 'campusfix-media-bucket',
      'X-Amz-Algorithm': 'AWS4-HMAC-SHA256',
      'X-Amz-Credential': 'AKIAIOSFODNN7EXAMPLE/20261122/ap-southeast-2/s3/aws4_request',
    };

    const res = await uploadToPresignedS3('https://campusfix-media-bucket.s3.ap-southeast-2.amazonaws.com/', fields, file);
    expect(res.status).toBe(204);

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = mockFetch.mock.calls[0];
    expect(calledUrl).toBe('https://campusfix-media-bucket.s3.ap-southeast-2.amazonaws.com/');
    expect(calledOptions.method).toBe('POST');

    // CRITICAL: Must NEVER attach Authorization header
    expect(calledOptions.headers).toBeUndefined();

    // Verify FormData construction
    const body = calledOptions.body as FormData;
    expect(body.get('key')).toBe('uploads/complaint-123.jpg');
    expect(body.get('bucket')).toBe('campusfix-media-bucket');
    expect(body.get('X-Amz-Algorithm')).toBe('AWS4-HMAC-SHA256');
    expect(body.get('file')).toBeInstanceOf(File);

    // Verify ordering: file must be the last field appended
    const keys: string[] = [];
    body.forEach((_, key) => keys.push(key));
    expect(keys[keys.length - 1]).toBe('file');
  });

  it('throws an error when presigned S3 upload fails', async () => {
    mockFetch.mockResolvedValueOnce(new Response('Access Denied', { status: 403, statusText: 'Forbidden' }));

    const file = new File(['test'], 'test.png', { type: 'image/png' });
    await expect(uploadToPresignedS3('https://s3.example.com', {}, file)).rejects.toThrow(
      'S3 direct upload failed with status 403 Forbidden'
    );
  });
});
