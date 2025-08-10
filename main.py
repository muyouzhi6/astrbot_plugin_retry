# --- START OF FILE main.py ---

import asyncio
import json

import astrbot.api.message_components as Comp
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
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

    async def _perform_retry(self, prompt: str, session_str: str, image_urls: list, func_tool: any):
        """
        根据具体信息，重新构建一个完整的请求并执行。
        session_str 格式为 platform:type:id
        """
        provider = self.context.get_using_provider()
        if not provider:
            logger.warning("LLM提供商未启用，无法重试。")
            return None
            
        try:
            # 从完整的 session 字符串中提取原始 ID
            parts = session_str.split(":")
            if len(parts) != 3:
                logger.error(f"[Retry] session 字符串格式错误: {session_str}")
                return None
                
            raw_session_id = parts[2]  # 使用最后一部分作为原始 ID
            
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

    async def _retry_task(self, prompt: str, session_str: str, image_urls: list, func_tool: any):
        """独立的后台任务，接收具体的数据，与 event 对象生命周期解耦"""
        # 检查 session 字符串格式
        parts = str(session_str).split(":")
        if len(parts) != 3:
            logger.error(f"[Retry] session 字符串格式错误: {session_str}")
            return
        
        for attempt in range(1, self.max_attempts + 1):
            logger.info(f"[Retry] 后台重试任务: 第 {attempt}/{self.max_attempts} 次尝试... session: {session_str}")
            new_response = await self._perform_retry(prompt, session_str, image_urls, func_tool)

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
                logger.info(f"[Retry] 后台重试成功，正在直接发送新回复到会话: {session_str}，内容: {new_text[:100]}...")
                try:
                    if not new_text.strip():
                        logger.error(f"[Retry] 生成回复内容为空，放弃发送。session: {session_str}")
                        return
                    # 确保 session 字符串格式正确并进行消息发送
                    if session_str and session_str.count(":") == 2:
                        await self.context.send_message(new_text, session_str)
                        logger.info(f"[Retry] 新回复已成功发送。session: {session_str}")
                        return
                    else:
                        logger.error(f"[Retry] session 字符串格式错误，无法发送消息: {session_str}")
                        return
                except Exception as e:
                    logger.error(f"[Retry] 后台重试成功，但发送消息时发生错误: {e}，session: {session_str}，内容: {new_text}", exc_info=True)
                    return

            if attempt < self.max_attempts:
                logger.warning(f"[Retry] 后台重试尝试失败，将在 {self.retry_delay} 秒后进行下一次尝试... session: {session_str}")
                await asyncio.sleep(self.retry_delay)

        logger.error(f"[Retry] 所有 {self.max_attempts} 次后台重试均失败，放弃该次回复。session: {session_str}")
        # 可选：重试全部失败后，主动通知用户
        if session_str and session_str.count(":") == 2:
            try:
                await self.context.send_message("很抱歉，AI 回复失败，请稍后再试。", session_str)
                logger.info(f"[Retry] 已通知用户回复失败。session: {session_str}")
            except Exception as e:
                logger.error(f"[Retry] 通知用户失败时发生异常: {e}，session: {session_str}", exc_info=True)
        else:
            logger.error(f"[Retry] session 字符串格式错误，无法发送失败通知: {session_str}")

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
            
            # 1. 检查是否包含任何已知的错误关键词
            is_error = any(keyword in lower_message_str for keyword in self.error_keywords)
            
            # 2. 判断是否为典型的错误响应格式
            is_error_format = (
                ("错误类型:" in message_str and "错误信息:" in message_str) or
                "astrbot 请求失败" in lower_message_str or
                "请在控制台查看和分享错误详情" in message_str
            )
            
            # 如果符合错误特征，就触发重试
            if is_error and is_error_format:
                logger.warning(f"检测到系统错误响应，准备启动后台重试: '{message_str}'")
                should_retry = True
                
        # 检查是否是真正的空回复（并且已经排除了tool_calls的情况）
        if not should_retry and is_truly_empty:
            # 获取原始的 LLM 响应状态
            llm_error = getattr(_llm_response, 'error', None)
            if llm_error:
                logger.warning(f"检测到LLM响应错误（非工具调用），准备启动后台重试。错误: {llm_error}")
                should_retry = True
            else:
                logger.warning("检测到最终输出为空（非工具调用），准备启动后台重试。")
                should_retry = True

        if should_retry:
            # 即使是图片消息没有文本，也应该重试，因为图片URL会被传递
            logger.info("创建后台重试任务，并立即阻止当前错误/空回复的发送。")

            # 获取用户的原始输入，包括文本和图片
            prompt_to_retry = getattr(event, 'message_str', '') or ''
            
            # 构建会话字符串
            session_to_use = None
            
            # 1. 首先尝试从 unified_msg_origin 获取，如果它已经包含完整格式
            if hasattr(event, "unified_msg_origin"):
                origin = str(event.unified_msg_origin)
                parts = origin.split(":")
                # 如果已经是标准格式，直接使用最后一组作为实际ID
                if len(parts) > 3:
                    last_parts = parts[-3:]  # 取最后三个部分
                    session_to_use = ":".join(last_parts)
                    logger.debug(f"[Retry] 从 unified_msg_origin 提取得到 session: {session_to_use}")
            
            # 2. 如果上面的方法失败，尝试从基本属性构建
            if not session_to_use:
                platform = "aiocqhttp"  # AstrBot 默认平台
                msg_type = "FriendMessage" if getattr(event, "message_type", "") == "private" else "GroupMessage"
                raw_id = None
                
                # 按优先级尝试获取 ID
                if hasattr(event, "user_id") and event.message_type == "private":
                    raw_id = str(event.user_id)
                elif hasattr(event, "group_id") and event.message_type == "group":
                    raw_id = str(event.group_id)
                
                if raw_id:
                    session_to_use = f"{platform}:{msg_type}:{raw_id}"
                    logger.debug(f"[Retry] 成功构建 session 字符串: {session_to_use}")
            
            # 3. 最后尝试从 session 属性获取
            if not session_to_use and hasattr(event, "session"):
                if isinstance(event.session, str):
                    session_to_use = event.session
                    logger.debug(f"[Retry] 从事件获取到 session 字符串: {session_to_use}")
                else:
                    # 可能是其他类型，尝试转换为字符串
                    try:
                        session_str = str(event.session)
                        # 验证并提取正确的格式
                        parts = session_str.split(":")
                        if len(parts) >= 3:
                            session_to_use = ":".join(parts[-3:])  # 取最后三个部分
                            logger.debug(f"[Retry] 从事件 session 对象转换得到字符串: {session_to_use}")
                    except Exception as e:
                        logger.error(f"[Retry] 转换 session 对象失败: {e}", exc_info=True)
            
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
            logger.debug(f"[Retry] 重试信息：prompt='{prompt_to_retry}', images={len(images_to_retry)}张")
            
            # 创建异步任务并保存引用以防止被垃圾回收
            retry_task = asyncio.create_task(self._retry_task(
                prompt_to_retry,
                session_to_use,
                images_to_retry,
                func_tool_to_retry 
            ))
            
            # 添加完成回调以记录任务状态
            def log_task_done(task):
                try:
                    task.result()  # 获取结果会重新抛出任务中的异常
                    logger.debug("[Retry] 后台重试任务已完成")
                except Exception as e:
                    logger.error(f"[Retry] 后台重试任务失败: {e}", exc_info=True)
            
            retry_task.add_done_callback(log_task_done)
            
            event.clear_result()
            event.stop_event()

    async def terminate(self):
        logger.info("已卸载 [IntelligentRetry] 插件。")

# --- END OF FILE main.py ---
