"""Independent OpenAI-compatible Pipecat services with safe provider errors."""

from __future__ import annotations

import base64
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, Literal

import httpx
import httpx2
from openai import APITimeoutError, AsyncOpenAI, AsyncStream, DefaultAsyncHttpxClient, omit
from openai._models import FinalRequestOptions
from openai.types.audio import Transcription
from openai.types.chat import ChatCompletionChunk
from pipecat.frames.frames import (
    AggregatedTextFrame,
    ErrorFrame,
    Frame,
    TTSAudioRawFrame,
    TTSStoppedFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.services.settings import TTSSettings as PipecatTTSSettings
from pipecat.services.tts_service import TTSService
from pipecat.transcriptions.language import Language

from llmautotel.models import AppSettings, ASRSettings, LLMSettings, ProviderSettings, TTSSettings

ProviderStage = Literal["asr", "llm", "tts"]


class ProviderFailure(RuntimeError):
    """Keep upstream response bodies and credentials outside pipeline errors."""

    def __init__(self, stage: ProviderStage, cause: Exception) -> None:
        status = getattr(cause, "status_code", None)
        if isinstance(cause, httpx.HTTPStatusError):
            status = cause.response.status_code
        detail = f"（HTTP {status}）" if isinstance(status, int) else ""
        if isinstance(
            cause, (httpx.TimeoutException, httpx2.TimeoutException, APITimeoutError, TimeoutError)
        ):
            detail = "（超时）"
        super().__init__(f"{stage}: 模型请求失败{detail}")
        self.stage = stage


def _key(settings: ProviderSettings) -> str:
    return settings.api_key.get_secret_value() if settings.api_key is not None else ""


async def _no_auth_key() -> str:
    """The SDK accepts a callback while rejecting an initially empty key."""
    return ""


def _sdk_key(settings: ProviderSettings) -> str | Callable[[], Awaitable[str]]:
    return _key(settings) or _no_auth_key


def _sdk_http_client(settings: ProviderSettings) -> httpx2.AsyncClient:
    """Use the HTTP client family belonging to the locked OpenAI SDK."""
    return DefaultAsyncHttpxClient(timeout=settings.timeout_seconds)


class _CompatibleOpenAIClient(AsyncOpenAI):
    """SDK 3 requires a per-request explicit auth omission for unkeyed servers."""

    async def _prepare_options(self, options: FinalRequestOptions) -> FinalRequestOptions:
        options = await super()._prepare_options(options)
        if not self.api_key:
            options.headers = {**(options.headers or {}), "Authorization": omit}
        return options


class CompatibleSTTService(OpenAISTTService):
    """Reuse Pipecat's segmented WAV transcription and language mapping."""

    def __init__(
        self, settings: ASRSettings, *, http_client: httpx2.AsyncClient | None = None
    ) -> None:
        client = http_client or _sdk_http_client(settings)
        self._provider_settings = settings.model_copy(deep=True)
        super().__init__(
            api_key=_sdk_key(settings),
            base_url=settings.base_url,
            http_client=client,
            settings=OpenAISTTService.Settings(
                model=settings.model,
                language=Language(settings.language) if settings.language != "auto" else None,
            ),
            sample_rate=16000,
            push_empty_transcripts=True,
        )
        # The STT constructor exposes its HTTP client but not SDK retry options.
        self._client = _CompatibleOpenAIClient(
            api_key=_sdk_key(settings),
            base_url=settings.base_url,
            http_client=client,
            max_retries=0,
            timeout=settings.timeout_seconds,
        )

    async def _transcribe(self, audio: bytes) -> Transcription:
        try:
            if self._provider_settings.protocol == "mimo":
                encoded = base64.b64encode(audio).decode("ascii")
                # MiMo 的限制针对编码后的输入，不将超大片段发送给服务端。
                if len(encoded) > 10 * 1024 * 1024:
                    raise ValueError("ASR audio exceeds the protocol limit")
                completion = await self._client.chat.completions.create(
                    model=self._provider_settings.model,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_audio",
                                    "input_audio": {"data": f"data:audio/wav;base64,{encoded}"},
                                }
                            ],
                        }
                    ],
                    extra_body={"asr_options": {"language": self._provider_settings.language}},
                    stream=False,
                )
                return Transcription(text=completion.choices[0].message.content or "")
            return await super()._transcribe(audio)
        except Exception as error:
            raise ProviderFailure("asr", error) from None

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        async for frame in super().run_stt(audio):
            if isinstance(frame, ErrorFrame):
                # Pipecat adds its generic prefix around the already-sanitized error.
                error_text = frame.error.partition("asr:")[2]
                yield ErrorFrame(error=f"asr:{error_text}" if error_text else "asr: 模型请求失败")
            else:
                yield frame

    async def aclose(self) -> None:
        await self._client.close()

    async def cleanup(self) -> None:
        try:
            await super().cleanup()
        finally:
            await self.aclose()


class CompatibleLLMService(OpenAILLMService):
    """Use the official streaming LLM service with zero retries and safe errors."""

    def __init__(
        self, settings: LLMSettings, *, http_client: httpx2.AsyncClient | None = None
    ) -> None:
        self._provider_http_client = http_client or _sdk_http_client(settings)
        self._provider_timeout = settings.timeout_seconds
        extra: dict[str, Any] = {}
        if settings.reasoning_effort is not None:
            extra["reasoning_effort"] = settings.reasoning_effort
        if settings.thinking is not None:
            extra["extra_body"] = {"thinking": {"type": settings.thinking}}
        super().__init__(
            api_key=_sdk_key(settings),
            base_url=settings.base_url,
            settings=OpenAILLMService.Settings(model=settings.model, extra=extra),
            retry_on_timeout=False,
        )

    def create_client(
        self,
        api_key: str | Callable[[], Awaitable[str]] | None = None,
        base_url: str | None = None,
        organization: str | None = None,
        project: str | None = None,
        default_headers: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> AsyncOpenAI:
        return _CompatibleOpenAIClient(
            api_key=api_key,
            base_url=base_url,
            organization=organization,
            project=project,
            default_headers=default_headers,
            http_client=self._provider_http_client,
            timeout=self._provider_timeout,
            max_retries=0,
        )

    async def get_chat_completions(self, context: LLMContext) -> AsyncStream[ChatCompletionChunk]:
        try:
            return await super().get_chat_completions(context)
        except Exception as error:
            raise ProviderFailure("llm", error) from None

    async def _process_context(self, context: LLMContext) -> None:
        try:
            await super()._process_context(context)
        except ProviderFailure:
            raise
        except Exception as error:
            raise ProviderFailure("llm", error) from None

    async def aclose(self) -> None:
        await self._client.close()

    async def cleanup(self) -> None:
        try:
            await super().cleanup()
        finally:
            await self.aclose()


class CompatibleTTSService(TTSService):
    """Thin PCM adapter allowing provider-specific voice identifiers."""

    def __init__(
        self,
        settings: TTSSettings,
        *,
        http_client: httpx.AsyncClient | None = None,
        mimo_http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            settings=PipecatTTSSettings(
                model=settings.model, voice=settings.voice, language=Language.ZH
            ),
            sample_rate=settings.sample_rate,
            push_start_frame=True,
            push_stop_frames=True,
            reuse_context_id_within_turn=False,
        )
        self._provider_settings = settings.model_copy(deep=True)
        self._http_client = (
            http_client or httpx.AsyncClient(timeout=settings.timeout_seconds)
            if settings.protocol == "openai"
            else None
        )
        self._mimo_client = (
            _CompatibleOpenAIClient(
                api_key=_sdk_key(settings),
                base_url=settings.base_url,
                http_client=mimo_http_client or _sdk_http_client(settings),
                max_retries=0,
                timeout=settings.timeout_seconds,
            )
            if settings.protocol == "mimo"
            else None
        )
        self._endpoint = f"{settings.base_url.rstrip('/')}/audio/speech"

    async def _push_tts_frames(
        self,
        src_frame: AggregatedTextFrame,
        includes_inter_frame_spaces: bool = False,
        append_tts_text_to_context: bool = True,
        push_assistant_aggregation: bool | None = False,
    ) -> None:
        await super()._push_tts_frames(
            src_frame,
            includes_inter_frame_spaces=includes_inter_frame_spaces,
            append_tts_text_to_context=append_tts_text_to_context,
            push_assistant_aggregation=push_assistant_aggregation,
        )
        # Each HTTP request is one sentence. Close it after Pipecat enqueues
        # its TTSTextFrame, rather than waiting for the idle-context watchdog.
        context_id = src_frame.context_id
        if context_id and self.audio_context_available(context_id):
            await self.append_to_audio_context(context_id, TTSStoppedFrame(context_id=context_id))
            await self.remove_audio_context(context_id)

    async def _audio_chunks(self, text: str) -> AsyncGenerator[bytes, None]:
        if self._mimo_client is not None:
            async with await self._mimo_client.chat.completions.create(
                model=self._provider_settings.model,
                messages=[{"role": "assistant", "content": text}],
                audio={"format": "pcm16", "voice": self._provider_settings.voice},
                stream=True,
            ) as stream:
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    audio = getattr(chunk.choices[0].delta, "audio", None)
                    if audio is None:
                        continue
                    if not isinstance(audio, dict):
                        raise ValueError("Invalid audio delta")
                    encoded = audio.get("data")
                    if encoded is None:
                        continue
                    if not isinstance(encoded, str):
                        raise ValueError("Invalid audio data")
                    yield base64.b64decode(encoded, validate=True)
            return
        assert self._http_client is not None
        headers = (
            {"Authorization": f"Bearer {_key(self._provider_settings)}"}
            if _key(self._provider_settings)
            else {}
        )
        payload = {
            "input": text,
            "model": self._provider_settings.model,
            "voice": self._provider_settings.voice,
            "response_format": "pcm",
        }
        async with self._http_client.stream(
            "POST", self._endpoint, headers=headers, json=payload,
            timeout=self._provider_settings.timeout_seconds,
        ) as response:
            response.raise_for_status()
            media_type = response.headers.get("content-type", "").split(";", 1)[0].lower().strip()
            if media_type not in {"", "application/octet-stream", "audio/pcm", "audio/raw"}:
                raise ValueError("服务返回的音频格式不是原始 PCM")
            async for chunk in response.aiter_bytes():
                yield chunk

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        try:
            # aclosing 保证消费者在 yield 处取消时也立即关闭 SDK / HTTP 音频流。
            async with aclosing(self._audio_chunks(text)) as chunks:
                remainder = b""
                emitted = False
                async for chunk in chunks:
                    # HTTP chunks can cut a 16-bit sample in half. Keep that byte
                    # for the next chunk instead of emitting malformed audio.
                    remainder += chunk
                    aligned = len(remainder) & ~1
                    if aligned:
                        emitted = True
                        yield TTSAudioRawFrame(
                            audio=remainder[:aligned],
                            sample_rate=self._provider_settings.sample_rate,
                            num_channels=1,
                            context_id=context_id,
                        )
                        remainder = remainder[aligned:]
                if remainder:
                    yield ErrorFrame(error="tts: PCM 音频末尾存在不完整的 16 位采样")
                elif not emitted:
                    yield ErrorFrame(error="tts: 服务未返回 PCM 音频")
        except Exception as error:
            yield ErrorFrame(error=str(ProviderFailure("tts", error)))

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
        if self._mimo_client is not None:
            await self._mimo_client.close()

    async def cleanup(self) -> None:
        try:
            await super().cleanup()
        finally:
            await self.aclose()


@dataclass
class ProviderServices:
    stt: CompatibleSTTService
    llm: CompatibleLLMService
    tts: CompatibleTTSService

    async def aclose(self) -> None:
        """Release clients when pipeline setup did not finish."""
        await self.stt.aclose()
        await self.llm.aclose()
        await self.tts.aclose()


def create_services(settings: AppSettings) -> ProviderServices:
    """Create one independent service/client for each configured model."""
    return ProviderServices(
        stt=CompatibleSTTService(settings.asr),
        llm=CompatibleLLMService(settings.llm),
        tts=CompatibleTTSService(settings.tts),
    )
