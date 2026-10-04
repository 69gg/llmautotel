"""验证被打断的生成稿进入背景，同时不污染已播放记录或恢复旧音频。"""

from __future__ import annotations

from copy import deepcopy

import pytest
from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    LLMAssistantPushAggregationFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    TTSTextFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.utils.text.base_text_aggregator import AggregationType

from llmautotel.conversation import (
    INTERRUPTED_BACKGROUND_LABEL,
    InterruptedResponse,
    InterruptedResponseContext,
)


def context_with_question() -> LLMContext:
    return LLMContext(
        [
            {"role": "system", "content": "直接回应最新用户问题。"},
            {"role": "user", "content": "多少钱？"},
        ]
    )


def begin_response(state: InterruptedResponseContext, text: str) -> LLMFullResponseStartFrame:
    start = LLMFullResponseStartFrame()
    state._after_llm(start)
    state._after_llm(LLMTextFrame(text))
    state._after_output(start)
    return start


def commit_new_question(state: InterruptedResponseContext, question: str) -> None:
    state.context.add_message({"role": "user", "content": question})
    state._before_llm(LLMContextFrame(state.context))


def test_partial_generated_price_becomes_background_before_latest_question() -> None:
    context = context_with_question()
    state = InterruptedResponseContext(context)
    begin_response(state, "每月 20 美元，人民币金额以")
    state._before_llm(InterruptionFrame())
    commit_new_question(state, "人民币大概多少钱？")

    messages = context.get_messages()
    assert messages[-1] == {"role": "user", "content": "人民币大概多少钱？"}
    assert messages[-2] == {
        "role": "assistant",
        "content": f"{INTERRUPTED_BACKGROUND_LABEL}\n每月 20 美元，人民币金额以",
    }
    assert messages[1] == {"role": "user", "content": "多少钱？"}
    assert state.last_interrupted == InterruptedResponse("每月 20 美元，人民币金额以", "")
    assert state.generation == 1


def test_completed_sentence_remains_normal_context_without_duplicated_background() -> None:
    context = context_with_question()
    state = InterruptedResponseContext(context)
    begin_response(state, "每月 20 美元。最终金额以结算页面为准。")
    played = TTSTextFrame("每月 20 美元。", AggregationType.SENTENCE)
    state._after_output(played)
    # 正常的完整句仍由 Pipecat 助手聚合器提交。
    context.add_message({"role": "assistant", "content": played.text})
    state._before_llm(InterruptionFrame())
    commit_new_question(state, "人民币呢？")

    messages = context.get_messages()
    assert messages[-3] == {"role": "assistant", "content": "每月 20 美元。"}
    assert messages[-2]["content"] == (
        f"{INTERRUPTED_BACKGROUND_LABEL}\n最终金额以结算页面为准。"
    )
    assert messages[-1]["content"] == "人民币呢？"
    assert state.last_interrupted is not None
    assert state.last_interrupted.played_text == "每月 20 美元。"


def test_generation_end_does_not_discard_draft_while_audio_is_still_playing() -> None:
    context = context_with_question()
    state = InterruptedResponseContext(context)
    begin_response(state, "这句已经生成完，但还在播放。")
    end = LLMFullResponseEndFrame()
    state._after_llm(end)
    # 结束帧尚未穿过输出队列，打断仍应保留生成稿。
    state._before_llm(InterruptionFrame())
    commit_new_question(state, "先等一下。")
    assert "这句已经生成完，但还在播放。" in context.get_messages()[-2]["content"]


def test_multiple_played_sentences_match_even_when_aggregator_inserts_spaces() -> None:
    state = InterruptedResponseContext(context_with_question())
    begin_response(state, "第一句。第二句。第三句尚未播放")
    state._after_output(TTSTextFrame("第一句。", AggregationType.SENTENCE))
    state._after_output(TTSTextFrame("第二句。", AggregationType.SENTENCE))
    state._before_llm(InterruptionFrame())
    commit_new_question(state, "换算成人民币。")
    assert state.context.get_messages()[-2]["content"] == (
        f"{INTERRUPTED_BACKGROUND_LABEL}\n第三句尚未播放"
    )


def test_user_transcription_is_not_counted_as_played_assistant_content() -> None:
    state = InterruptedResponseContext(context_with_question())
    begin_response(state, "这条回复还没说出来")
    state._after_output(TranscriptionFrame("这条回复还没说出来", "user", "now"))
    state._before_llm(InterruptionFrame())
    commit_new_question(state, "当前问题。")
    assert state.context.get_messages()[-2]["content"] == (
        f"{INTERRUPTED_BACKGROUND_LABEL}\n这条回复还没说出来"
    )


@pytest.mark.parametrize("fixed_opening", [False, True])
def test_fully_output_response_is_not_added_as_interrupted_background(
    fixed_opening: bool,
) -> None:
    context = context_with_question()
    state = InterruptedResponseContext(context)
    if fixed_opening:
        state._after_llm(TTSSpeakFrame("您好，我是 AI 助手。"))
        state._after_output(TTSStartedFrame())
        state._after_output(TTSTextFrame("您好，我是 AI 助手。", AggregationType.SENTENCE))
        state._after_output(LLMAssistantPushAggregationFrame())
    else:
        begin_response(state, "这句已播放完整。")
        state._after_output(TTSTextFrame("这句已播放完整。", AggregationType.SENTENCE))
        end = LLMFullResponseEndFrame()
        state._after_llm(end)
        state._after_output(end)
    state._before_llm(InterruptionFrame())
    commit_new_question(state, "换个问题。")
    assert all(
        not str(message.get("content", "")).startswith(INTERRUPTED_BACKGROUND_LABEL)
        for message in context.get_messages()
    )


def test_fixed_opening_interrupted_before_audio_becomes_background() -> None:
    context = context_with_question()
    state = InterruptedResponseContext(context)
    state._after_llm(TTSSpeakFrame("您好，考虑订阅测试产品吗？"))
    state._before_llm(InterruptionFrame())
    commit_new_question(state, "这个多贵？")
    assert context.get_messages()[-2]["content"] == (
        f"{INTERRUPTED_BACKGROUND_LABEL}\n您好，考虑订阅测试产品吗？"
    )


def test_repeated_interruptions_do_not_duplicate_background_or_accept_old_tokens() -> None:
    context = context_with_question()
    state = InterruptedResponseContext(context)
    begin_response(state, "旧回复：每月 20 美元，")
    state._before_llm(InterruptionFrame())
    state._after_llm(LLMTextFrame("迟到的旧介绍。"))
    state._before_llm(InterruptionFrame())
    commit_new_question(state, "这个多贵呀！")
    first_context = deepcopy(context.get_messages())
    state._before_llm(LLMContextFrame(context))
    assert context.get_messages() == first_context

    begin_response(state, "新回复：您可以根据自己的需要")
    state._before_llm(InterruptionFrame())
    state._after_llm(LLMTextFrame("第二次迟到的旧回复。"))
    commit_new_question(state, "人民币大概多少钱？")
    backgrounds = [
        message["content"]
        for message in context.get_messages()
        if str(message.get("content", "")).startswith(INTERRUPTED_BACKGROUND_LABEL)
    ]
    assert len(backgrounds) == 2
    assert "旧回复：每月 20 美元，" in backgrounds[0]
    assert "新回复：您可以根据自己的需要" in backgrounds[1]
    assert "迟到" not in str(context.get_messages())
    assert state.generation == 3
    assert context.get_messages()[-1]["content"] == "人民币大概多少钱？"


def test_old_output_end_cannot_discard_new_response_background() -> None:
    state = InterruptedResponseContext(context_with_question())
    begin_response(state, "第一次生成的回复。")
    old_end = LLMFullResponseEndFrame()
    state._after_llm(old_end)
    state._before_llm(InterruptionFrame())
    commit_new_question(state, "换个问题。")
    begin_response(state, "第二次的未完回复")
    state._after_output(old_end)
    state._before_llm(InterruptionFrame())
    commit_new_question(state, "现在回答人民币价格。")
    assert "第二次的未完回复" in state.context.get_messages()[-2]["content"]


def test_vad_state_and_empty_interruptions_do_not_invent_background() -> None:
    state = InterruptedResponseContext(context_with_question())
    state._before_llm(VADUserStartedSpeakingFrame())
    assert state.user_speaking
    state._before_llm(InterruptionFrame())
    state._before_llm(VADUserStoppedSpeakingFrame())
    assert not state.user_speaking
    original = deepcopy(state.context.get_messages())
    state._before_llm(LLMContextFrame(state.context))
    assert state.context.get_messages() == original
    assert state.generation == 1


def test_interruption_changes_generation_before_next_request_updates_request_generation() -> None:
    state = InterruptedResponseContext(context_with_question())
    assert state.request_generation == state.generation == 0
    state._before_llm(LLMContextFrame(state.context))
    state._before_llm(InterruptionFrame())
    assert state.generation == 1
    assert state.request_generation == 0
    state._before_llm(VADUserStartedSpeakingFrame())
    state._before_llm(VADUserStoppedSpeakingFrame())
    assert state.request_generation == 0
    commit_new_question(state, "这一次先不要结束。")
    assert state.request_generation == state.generation == 1


async def test_input_processor_commits_before_forwarding_request_and_ignores_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = InterruptedResponseContext(context_with_question())
    begin_response(state, "仅作为背景的价格草稿")
    state._before_llm(InterruptionFrame())
    state.context.add_message({"role": "user", "content": "现在的问题。"})
    forwarded: list[tuple[Frame, FrameDirection]] = []

    async def forward(frame: Frame, direction: FrameDirection) -> None:
        if direction == FrameDirection.DOWNSTREAM:
            assert state.context.get_messages()[-2]["content"].startswith(
                INTERRUPTED_BACKGROUND_LABEL
            )
        forwarded.append((frame, direction))

    monkeypatch.setattr(state.input(), "push_frame", forward)
    request = LLMContextFrame(state.context)
    await state.input().process_frame(request, FrameDirection.DOWNSTREAM)
    committed = deepcopy(state.context.get_messages())
    await state.input().process_frame(request, FrameDirection.UPSTREAM)
    assert state.context.get_messages() == committed
    assert forwarded == [
        (request, FrameDirection.DOWNSTREAM),
        (request, FrameDirection.UPSTREAM),
    ]
    assert isinstance(state.generated(), FrameProcessor)
    assert isinstance(state.output(), FrameProcessor)
