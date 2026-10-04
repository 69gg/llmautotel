import type { Settings, SettingsUpdate } from './types';

export class ApiError extends Error {
  constructor(message: string, public readonly status: number) {
    super(message);
  }
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, init);
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new ApiError('无法连接服务，请确认后端已启动。', 0);
  }
  if (!response.ok) {
    const body: { detail?: unknown } = await response.json().catch(() => ({}));
    const detail = typeof body.detail === 'string' ? body.detail : '请检查填写内容后重试。';
    throw new ApiError(detail, response.status);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const api = {
  settings: (signal?: AbortSignal) => request<Settings>('/api/settings', { signal }),
  saveSettings: (settings: SettingsUpdate) => request<Settings>('/api/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(settings),
  }),
};
