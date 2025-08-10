# --- START OF FILE main.py ---

import asyncio
import json

import astrbot.api.message_components as Comp
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter, MessageSession  # MessageSession 应该在这里
from astrbot.api.star import Context, Star, register

@register(
    "intelligent_retry",
    "木有知 & 长安某",
    "当LLM最终回复为空或错误时进行重试",
    "2.4.0" # [根本性修正] 修复了对 tool_calls 的错误劫持，采用更可靠的事件响应判断
)
class IntelligentRetry(Star):
    """
    一个AstrBot插件，通过重新构建请求的方式，实现带上下文的重试。
    V2.4.0: 彻底修复了插件会错误地中断并重试正常 tool_calls 流程的问题。
            现在插件会直接检查事件的原始LLM响应，如果为工具调用则不进行任何干预，
            将控制权完全交还给框架，从根本上解决了连锁错误。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.max_attempts = config.get('max_attempts', 3)
        self.retry_delay = config.get('retry_delay', 2)
        default_keywords = "api 返回的内容为空\n请求失败\nAPI 返回的 completion 由于内容安全过滤被拒绝(非 AstrBot)\n调用失败\n处理失败"
        keywords_str = config.get('error_keywords', default_keywords)
        self.error_keywords = [k.strip().lower() for k in keywords_str.split('\n') if k.strip()]
        
        logger.info(
            f"已加载 [IntelligentRetry] 插件 v2.4.0, "
            f"将在LLM最终回复无效时自动重试 (最多 {self.max_attempts} 次)。"
        )

    async def _perform_retry(self, prompt: str, session: MessageSession, image_urls: list, func_tool: any):
        """
        根据具体信息，重新构建一个完整的请求并执行。
        session: MessageSession 对象
        """
        provider = self.context.get_using_provider()
        if not provider:
            logger.warning("LLM提供商未启用，无法重试。")
            return None
            
        try:
            raw_session_id = session.session_id  # 直接使用 session_id
            
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(raw_session_id)
            context_history = []
            if curr_cid:
                conv = await self.context.conversation_manager.get_conversation(raw_session_id, curr_cid)
                if conv and conv.history:
                    full_history = await asyncio.to_thread(json.loads, conv.history)
                    context_history = full_history[:-1] if full_history else []

            system_prompt = provider.system_prompt if hasattr(provider, "system_prompt") else None
            if not system_prompt and hasattr(provider, "config"):
                 system_prompt = provider.config.get("system_prompt")

            logger.debug(f"正在重新构建请求进行重试... Prompt: '{prompt}'")
            llm_response = await provider.text_chat(
                prompt=prompt, contexts=context_history, image_urls=image_urls,
                system_prompt=system_prompt, func_tool=func_tool
            )
            return llm_response
        except Exception as e:
            logger.error(f"重试调用LLM时发生错误: {e}", exc_info=True)
            return None

    async def _retry_task(self, prompt: str, session: MessageSession, image_urls: list, func_tool: any):
        """独立的后台任务，接收具体的数据，与 event 对象生命周期解耦"""
        session_desc = f"{session.platform_name}:{session.message_type}:{session.session_id}"
        
        for attempt in range(1, self.max_attempts + 1):
            logger.info(f"[Retry] 后台重试任务: 第 {attempt}/{self.max_attempts} 次尝试... session: {session_desc}")
            new_response = await self._perform_retry(prompt, session, image_urls, func_tool)

            new_text = ""
            if new_response and hasattr(new_response, 'completion_text'):
                new_text = new_response.completion_text.strip()

            is_new_reply_ok = False
            if new_text and not any(keyword in new_text.lower() for keyword in self.error_keywords):
                is_new_reply_ok = True

            finish_reason = 'stop'
            if new_response and hasattr(new_response, 'raw') and hasattr(new_response.raw, 'choices') and new_response.raw.choices:
                finish_reason = getattr(new_response.raw.choices[0], 'finish_reason', 'stop')

            if not new_text and finish_reason == 'stop':
                is_new_reply_ok = False

            if is_new_reply_ok:
                logger.info(f"[Retry] 后台重试成功，正在直接发送新回复到会话: {session_desc}，内容: {new_text[:100]}...")
                try:
                    if not new_text.strip():
                        logger.error(f"[Retry] 生成回复内容为空，放弃发送。session: {session_desc}")
                        return
                    await self.context.send_message(new_text, session)  # 使用 MessageSession 对象
                    logger.info(f"[Retry] 新回复已成功发送。session: {session_desc}")
                    return
                except Exception as e:
                    logger.error(f"[Retry] 后台重试成功，但发送消息时发生错误: {e}，session: {session_desc}，内容: {new_text}", exc_info=True)
                    return

            if attempt < self.max_attempts:
                logger.warning(f"[Retry] 后台重试尝试失败，将在 {self.retry_delay} 秒后进行下一次尝试... session: {session_desc}")
                await asyncio.sleep(self.retry_delay)

        logger.error(f"[Retry] 所有 {self.max_attempts} 次后台重试均失败，放弃该次回复。session: {session_desc}")
        # 可选：重试全部失败后，主动通知用户
        try:
            await self.context.send_message("很抱歉，AI 回复失败，请稍后再试。", session)  # 使用 MessageSession 对象
            logger.info(f"[Retry] 已通知用户回复失败。session: {session_desc}")
        except Exception as e:
            logger.error(f"[Retry] 通知用户失败时发生异常: {e}，session: {session_desc}", exc_info=True)

    @filter.on_decorating_result(priority=-100)
    async def check_and_retry(self, event: AstrMessageEvent):
        """
        检测错误。只有在LLM的最终回复为空或包含错误时才触发重试。
        """
        if self.max_attempts <= 0:
            return

        # [核心修正] 直接从事件对象获取最原始的LLM响应来进行判断
        _llm_response = getattr(event, 'llm_response', None)
        if _llm_response and hasattr(_llm_response, 'choices') and _llm_response.choices:
            finish_reason = getattr(_llm_response.choices[0], 'finish_reason', None)
            # 如果是工具调用，这是正常中间步骤，插件绝不能干预
            if finish_reason == 'tool_calls':
                logger.debug("检测到正常的工具调用中间步骤，插件将不进行任何干预。")
                return # **直接退出，让框架核心继续处理**

        # --- 只有在不是 tool_calls 的情况下，才执行下面的错误/空回复检查 ---
        # 只对最终输出的空/错误回复触发重试
        result = event.get_result()
        is_truly_empty = not result or not result.chain
        if not is_truly_empty:
            is_truly_empty = not any(
                (isinstance(c, Comp.Plain) and c.text.strip()) or not isinstance(c, Comp.Plain)
                for c in result.chain
            )

        should_retry = False
        message_str = result.get_plain_text() if result else ""

        # 检查是否包含明确的错误关键词
        if message_str:
            lower_message_str = message_str.lower()
            if any(keyword in lower_message_str for keyword in self.error_keywords):
                logger.warning(f"检测到最终输出错误文本，准备启动后台重试: '{message_str}'")
                should_retry = True

        # 检查是否是真正的空回复（并且已经排除了tool_calls的情况）
        if not should_retry and is_truly_empty:
            logger.warning("检测到最终输出为空（非工具调用），准备启动后台重试。")
            should_retry = True

        if should_retry and event.message_str.strip():
            logger.info("创建后台重试任务，并立即阻止当前错误/空回复的发送。")

            prompt_to_retry = event.message_str
            
            # 获取会话对象
            session_to_use = None
            
            if hasattr(event, "session"):
                # 直接使用事件的 session 属性（应该是 MessageSession 对象）
                session_to_use = event.session
                logger.debug(f"[Retry] 从事件获取到 session: {session_to_use}")
            else:
                # 如果没有直接的 session 对象，构建一个新的
                try:
                    platform = "aiocqhttp"  # AstrBot 默认平台
                    msg_type = "FriendMessage" if getattr(event, "message_type", "") == "private" else "GroupMessage"
                    
                    raw_id = None
                    # 按优先级尝试获取 ID
                    if hasattr(event, "user_id") and event.message_type == "private":
                        raw_id = str(event.user_id)
                    elif hasattr(event, "group_id") and event.message_type == "group":
                        raw_id = str(event.group_id)
                    elif hasattr(event, "unified_msg_origin"):
                        raw_id = str(event.unified_msg_origin)

                    if raw_id:
                        session_to_use = MessageSession(
                            platform_name=platform,
                            message_type=msg_type,
                            session_id=raw_id
                        )
                        logger.debug(f"[Retry] 成功构建 session 对象: {session_to_use}")
                except Exception as e:
                    logger.error(f"[Retry] 构建 session 对象失败: {e}", exc_info=True)
            
            if not session_to_use:
                logger.error(f"[Retry] 无法获取或构建合法的 session。event: {event}")
                return

            images_to_retry = [
                comp.url
                for comp in event.message_obj.message
                if isinstance(comp, Comp.Image) and hasattr(comp, "url") and comp.url
            ]

            provider = self.context.get_using_provider()
            func_tool_to_retry = None
            if provider and hasattr(provider, "func_tool"):
                func_tool_to_retry = provider.func_tool

            logger.debug(f"[Retry] 创建后台重试任务，session: {session_to_use}")
            asyncio.create_task(self._retry_task(
                prompt_to_retry,
                session_to_use,
                images_to_retry,
                func_tool_to_retry 
            ))
            
            event.clear_result()
            event.stop_event()

    async def terminate(self):
        logger.info("已卸载 [IntelligentRetry] 插件。")

# --- END OF FILE main.py ---
