import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SettingsPage } from '../components/SettingsPage';
import { api } from '../api';
import { settingsUpdate } from '../types';
import { fixtureSettings } from './fixtures';

afterEach(() => vi.restoreAllMocks());

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

describe('供应商协议与思考参数', () => {
  it('默认保存保持 OpenAI 协议与服务默认思考，不预填连接参数', async () => {
    const user = userEvent.setup();
    const save = vi.spyOn(api, 'saveSettings').mockResolvedValue(fixtureSettings);
    render(<SettingsPage settings={fixtureSettings} onSave={vi.fn()} />);
    expect(screen.getByLabelText('语音识别接口协议')).toHaveValue('openai');
    expect(screen.getByLabelText('语音合成接口协议')).toHaveValue('openai');
    expect(screen.getByLabelText('思考模式')).toHaveValue('');
    expect(screen.getByLabelText('推理强度')).toHaveValue('');
    await user.click(screen.getAllByRole('button', { name: '保存配置' })[0]);
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    const body = save.mock.calls[0][0];
    expect(body.asr.protocol).toBe('openai');
    expect(body.tts.protocol).toBe('openai');
    expect(body.llm).toMatchObject({ thinking: null, reasoning_effort: null, base_url: '', model: '' });
    expect(body.llm).not.toHaveProperty('api_key');
  });

  it('ASR 与 TTS 协议独立切换，保留地址、模型、音色与现有密钥', async () => {
    const user = userEvent.setup();
    const settings = {
      ...fixtureSettings,
      asr: { ...fixtureSettings.asr, base_url: 'https://speech.test/v1', model: 'configured-asr' },
      tts: { ...fixtureSettings.tts, base_url: 'https://voice.test/v1', model: 'configured-tts', voice: 'configured-voice', api_key_set: true },
    };
    const save = vi.spyOn(api, 'saveSettings').mockResolvedValue(settings);
    render(<SettingsPage settings={settings} onSave={vi.fn()} />);
    await user.selectOptions(screen.getByLabelText('语音识别接口协议'), 'mimo');
    expect(screen.getByLabelText('语音合成接口协议')).toHaveValue('openai');
    await user.selectOptions(screen.getByLabelText('语音合成接口协议'), 'mimo');
    await user.clear(screen.getByLabelText('识别语言'));
    await user.type(screen.getByLabelText('识别语言'), 'auto');
    await user.selectOptions(screen.getByLabelText('思考模式'), 'disabled');
    await user.selectOptions(screen.getByLabelText('推理强度'), 'high');
    await user.click(screen.getAllByRole('button', { name: '保存配置' })[0]);
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    const body = save.mock.calls[0][0];
    expect(body.asr).toMatchObject({ protocol: 'mimo', base_url: settings.asr.base_url, model: settings.asr.model, language: 'auto' });
    expect(body.tts).toMatchObject({ protocol: 'mimo', base_url: settings.tts.base_url, model: settings.tts.model, voice: settings.tts.voice });
    expect(body.llm).toMatchObject({ thinking: 'disabled', reasoning_effort: 'high' });
    expect(body.asr).not.toHaveProperty('api_key');
    expect(body.tts).not.toHaveProperty('api_key');
  });

  it('从显式思考参数切回服务默认时提交 null，而不是保留原设置', async () => {
    const user = userEvent.setup();
    const settings = { ...fixtureSettings, llm: { ...fixtureSettings.llm, thinking: 'enabled' as const, reasoning_effort: 'max' as const } };
    const save = vi.spyOn(api, 'saveSettings').mockResolvedValue(fixtureSettings);
    render(<SettingsPage settings={settings} onSave={vi.fn()} />);
    await user.selectOptions(screen.getByLabelText('思考模式'), '');
    await user.selectOptions(screen.getByLabelText('推理强度'), '');
    await user.click(screen.getAllByRole('button', { name: '保存配置' })[0]);
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    expect(save.mock.calls[0][0].llm).toMatchObject({ thinking: null, reasoning_effort: null });
  });

  it('推理强度 none 可显式保存，与服务默认 null 区分', async () => {
    const user = userEvent.setup();
    const save = vi.spyOn(api, 'saveSettings').mockResolvedValue(fixtureSettings);
    render(<SettingsPage settings={fixtureSettings} onSave={vi.fn()} />);
    await user.selectOptions(screen.getByLabelText('推理强度'), 'none');
    await user.click(screen.getAllByRole('button', { name: '保存配置' })[0]);
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    expect(save.mock.calls[0][0].llm.reasoning_effort).toBe('none');
  });
});
