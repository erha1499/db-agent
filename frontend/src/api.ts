export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', 'X-DB-Agent-Client': 'web', ...options.headers },
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok)
    throw new Error(payload?.error?.message || '服务暂时不可用，请检查本机 Web 服务。');
  return payload as T;
}

export const message = (error: unknown) =>
  error instanceof Error ? error.message : '操作失败，请重试。';
