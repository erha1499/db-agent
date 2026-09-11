export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
  ) {
    super(message);
  }
}

export const authChangedEvent = 'db-agent-auth-changed';
let generation = 0;
let authenticated = false;
let pageSession = '';
const pending = new Set<AbortController>();
const channel =
  typeof BroadcastChannel === 'undefined' ? null : new BroadcastChannel('db-agent-auth');

// Only an in-memory gate and a cross-tab invalidation signal; never store credentials or results.
export function clearAccess(reason: string, broadcast = true) {
  authenticated = false;
  pageSession = '';
  generation += 1;
  for (const controller of pending) controller.abort();
  pending.clear();
  window.dispatchEvent(new CustomEvent(authChangedEvent, { detail: reason }));
  if (broadcast) channel?.postMessage('changed');
}
export function acceptAccess(sessionId: string) {
  pageSession = sessionId;
  authenticated = true;
}
export function announceLogin() {
  channel?.postMessage('changed');
}
channel?.addEventListener('message', () => {
  clearAccess('工作区状态已在其他页面更改，请重新连接。', false);
});

async function request<T>(
  path: string,
  options: RequestInit,
  decode: (response: Response) => Promise<T>,
): Promise<T> {
  const isAuth = path.startsWith('/auth/');
  if (!isAuth && !authenticated) throw new ApiError('工作区尚未连接。', 401);
  const started = generation;
  const controller = new AbortController();
  const abort = () => controller.abort();
  options.signal?.addEventListener('abort', abort, { once: true });
  if (options.signal?.aborted) controller.abort();
  pending.add(controller);
  try {
    const response = await fetch(`/api${path}`, {
      ...options,
      credentials: 'same-origin',
      cache: 'no-store',
      signal: controller.signal,
      headers: {
        'Content-Type': 'application/json',
        'X-DB-Agent-Client': 'web',
        ...options.headers,
        ...(!isAuth ? { 'X-DB-Agent-Session': pageSession } : {}),
      },
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => null);
      if (started !== generation) throw new DOMException('工作区连接已更改。', 'AbortError');
      if (response.status === 401 && !isAuth)
        clearAccess('工作区连接已失效或权限配置已变更，请重新连接。');
      throw new ApiError(
        payload?.error?.message || '服务暂时不可用，请检查本机 Web 服务。',
        response.status,
        payload?.error?.code,
      );
    }
    const value = await decode(response);
    if (started !== generation) throw new DOMException('工作区连接已更改。', 'AbortError');
    return value;
  } finally {
    pending.delete(controller);
    options.signal?.removeEventListener('abort', abort);
  }
}

export function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  return request(path, options, (response) => response.json());
}
export function apiBlob(path: string, options: RequestInit = {}): Promise<Blob> {
  return request(path, options, (response) => response.blob());
}
export const message = (error: unknown) =>
  error instanceof Error ? error.message : '操作失败，请重试。';
