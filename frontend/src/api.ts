import type { CallRecord, CallSummary, EndReason, Settings, SettingsUpdate, StartCallResult } from './types';

export class ApiError extends Error {
  constructor(message: string, public readonly status: number) {
    super(message);
  }
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const configuredTimeout = Number(import.meta.env.VITE_API_TIMEOUT_MS);
  const timeoutMs = Number.isFinite(configuredTimeout) && configuredTimeout >= 1 ? Math.floor(configuredTimeout) : 10_000;
  const deadline = AbortSignal.timeout(timeoutMs);
  const signal = init?.signal ? AbortSignal.any([init.signal, deadline]) : deadline;
  try {
    const response = await fetch(path, { ...init, signal });
    if (!response.ok) {
      const body: { detail?: unknown } = await response.json().catch(error => {
        if (signal.aborted) throw error;
        return {};
      });
      const detail = typeof body.detail === 'string' ? body.detail : '请检查填写内容后重试。';
      throw new ApiError(detail, response.status);
    }
    if (response.status === 204) return undefined as T;
    return await response.json() as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (init?.signal?.aborted) throw error;
    if (deadline.aborted && !init?.signal?.aborted) throw new ApiError('服务响应超时，请检查服务状态后重试。', 0);
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new ApiError('无法连接服务，请确认后端已启动。', 0);
  }
}

export const api = {
  settings: (signal?: AbortSignal) => request<Settings>('/api/settings', { signal }),
  saveSettings: (settings: SettingsUpdate) => request<Settings>('/api/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(settings),
  }),
  startCall: () => request<StartCallResult>('/api/calls', { method: 'POST' }),
  endCall: (id: string, reason: EndReason = 'user_hangup') => request<CallRecord>(`/api/calls/${encodeURIComponent(id)}/end`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ reason }) }),
  activeCall: () => request<CallRecord | null>('/api/calls/active'),
  calls: (signal?: AbortSignal) => request<CallSummary[]>('/api/calls', { signal }),
  call: (id: string, signal?: AbortSignal) => request<CallRecord>(`/api/calls/${encodeURIComponent(id)}`, { signal }),
  deleteCall: (id: string) => request<void>(`/api/calls/${encodeURIComponent(id)}`, { method: 'DELETE' }),
};
