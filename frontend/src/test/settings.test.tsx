import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { SettingsPage } from '../components/SettingsPage';
import { api } from '../api';
import { settingsUpdate } from '../types';
import { fixtureSettings } from './fixtures';

describe('配置中的密钥处理', () => {
  it('空输入保留密钥，显式清除发送 null，回读标记不进入提交', () => {
    const body = settingsUpdate(fixtureSettings, { asr: '', llm: 'new-secret', tts: null });
    expect(body.asr).not.toHaveProperty('api_key');
    expect(body.asr).not.toHaveProperty('api_key_set');
    expect(body.llm.api_key).toBe('new-secret');
    expect(body.tts.api_key).toBeNull();
    expect(body.voice).toEqual(fixtureSettings.voice);
  });

  it('保存后清空密钥输入，保存公开返回值，不写浏览器存储', async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    const save = vi.spyOn(api, 'saveSettings').mockResolvedValue({ ...fixtureSettings, llm: { ...fixtureSettings.llm, api_key_set: true } });
    const storage = vi.spyOn(Storage.prototype, 'setItem');
    render(<SettingsPage settings={fixtureSettings} onSave={onSave} />);
    const input = screen.getByLabelText('文本模型 API 密钥');
    await user.type(input, 'new-secret');
    await user.click(screen.getByRole('button', { name: '清除语音识别密钥' }));
    await user.click(screen.getAllByRole('button', { name: '保存配置' })[0]);
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(save.mock.calls[0][0].asr.api_key).toBeNull();
    expect(save.mock.calls[0][0].llm.api_key).toBe('new-secret');
    expect(input).toHaveValue('');
    expect(storage).not.toHaveBeenCalled();
    expect(screen.getByText('已保存，下次通话将使用新配置。')).toBeInTheDocument();
    vi.restoreAllMocks();
  });

  it('保存失败保留编辑内容并显示错误，允许再次保存', async () => {
    const user = userEvent.setup();
    const save = vi.spyOn(api, 'saveSettings').mockRejectedValue(new Error('连接失败'));
    render(<SettingsPage settings={fixtureSettings} onSave={vi.fn()} />);
    await user.type(screen.getByLabelText('销售目标'), '新目标');
    await user.click(screen.getAllByRole('button', { name: '保存配置' })[0]);
    expect(await screen.findByRole('alert')).toHaveTextContent('连接失败');
    expect(screen.getByLabelText('销售目标')).toHaveValue('介绍订阅服务新目标');
    expect(screen.getAllByRole('button', { name: '保存配置' })[0]).toBeEnabled();
    save.mockRestore();
  });
});
