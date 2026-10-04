import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, request } from '../api';

afterEach(() => vi.unstubAllGlobals());

describe('服务请求', () => {
  it('显示后端业务错误', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: '已有一通会话' }), { status: 409 })));
    await expect(request('/api/settings')).rejects.toThrow('已有一通会话');
  });

  it('校验错误不暴露请求中的密钥或数据', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: [{ input: 'secret-data' }] }), { status: 422 })));
    await expect(request('/api/settings')).rejects.toThrow('请检查填写内容后重试。');
  });

  it('网络故障提供可操作提示', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    await expect(request('/api/settings')).rejects.toEqual(new ApiError('无法连接服务，请确认后端已启动。', 0));
  });
});
