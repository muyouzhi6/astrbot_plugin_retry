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
    "2.3.2" # [修正] 修复后台重试时工具(tool)上下文丢失的问题
)
class IntelligentRetry(Star):
    """
    一个AstrBot插件，通过重新构建请求的方式，实现带上下文的重试。
    V2.3.2: 修正了后台重试任务无法获取到原始请求的工具(Tools)列表的问题。
            现在插件会捕获并传递工具上下文，确保重试时也能正确调用工具。
    V2.3.1: 修正了对'finish_reason'的判断，避免对tool_calls进行错误重试。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.max_attempts = config.get('max_attempts', 3)
        self.retry_delay = config.get('retry_delay', 2)
        default_keywords = "api 返回的内容为空\n请求失败\nAPI 返回的 completion 由于内容安全过滤被拒绝(非 AstrBot)\n调用失败\n处理失败"
        keywords_str = config.get('error_keywords', default_keywords)
        self.error_keywords = [k.strip().lower() for k in keywords_str.split('\n') if k.strip()]
        
        logger.info(
            f"已加载 [IntelligentRetry] 插件 v2.3.2, "
            f"将在LLM最终回复无效时自动重试 (最多 {self.max_attempts} 次)。"
        )

    async def _perform_retry(self, prompt: str, unified_msg_origin: str, image_urls: list, func_tool: any):
        """
        根据具体信息，重新构建一个完整的请求并执行。
        """
        provider = self.context.get_using_provider()
        if not provider:
            logger.warning("LLM提供商未启用，无法重试。")
            return None
            
        try:
            # 1. 获取对话历史
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(
                unified_msg_origin
            )
            context_history = []
            if curr_cid:
                conv = await self.context.conversation_manager.get_conversation(
                    unified_msg_origin, curr_cid
                )
                if conv and conv.history:
                    full_history = await asyncio.to_thread(json.loads, conv.history)
                    context_history = full_history[:-1] if full_history else []

            # 2. 获取 System Prompt (人设)
            system_prompt = provider.system_prompt if hasattr(provider, "system_prompt") else None
            if not system_prompt and hasattr(provider, "config"):
                 system_prompt = provider.config.get("system_prompt")

            logger.debug(f"正在重新构建请求进行重试... Prompt: '{prompt}'")
            llm_response = await provider.text_chat(
                prompt=prompt,
                contexts=context_history,
                image_urls=image_urls,
                system_prompt=system_prompt,
                func_tool=func_tool # 直接使用传递进来的工具列表
            )
            return llm_response
        except Exception as e:
            logger.error(f"重试调用LLM时发生错误: {e}", exc_info=True)
            return None

    async def _retry_task(self, prompt: str, unified_msg_origin: str, image_urls: list, func_tool: any):
        """独立的后台任务，接收具体的数据，与 event 对象生命周期解耦"""
        for attempt in range(1, self.max_attempts + 1):
            logger.info(f"后台重试任务: 第 {attempt}/{self.max_attempts} 次尝试...")
            # 将工具列表传递给执行函数
            new_response = await self._perform_retry(prompt, unified_msg_origin, image_urls, func_tool)

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
                logger.info(f"后台重试成功，正在直接发送新回复到会话: {unified_msg_origin}")
                try:
                    await self.context.send_message(new_text, unified_msg_origin)
                    return
                except Exception as e:
                    logger.error(f"后台重试成功，但发送消息时发生错误: {e}", exc_info=True)
                    return

            if attempt < self.max_attempts:
                logger.warning(f"后台重试尝试失败，将在 {self.retry_delay} 秒后进行下一次尝试...")
                await asyncio.sleep(self.retry_delay)
        
        logger.error(f"所有 {self.max_attempts} 次后台重试均失败，放弃该次回复。")

    @filter.on_decorating_result(priority=-1)
    async def check_and_retry(self, event: AstrMessageEvent):
        """
        检测错误。只有在LLM的最终回复为空或包含错误时才触发重试。
        """
        if self.max_attempts <= 0:
            return

        result = event.get_result()
        
        is_truly_empty = not result or not result.chain
        if not is_truly_empty:
            is_truly_empty = not any(
                (isinstance(c, Comp.Plain) and c.text.strip()) or not isinstance(c, Comp.Plain)
                for c in result.chain
            )
        
        should_retry = False
        message_str = result.get_plain_text() if result else ""

        if message_str:
            lower_message_str = message_str.lower()
            if any(keyword in lower_message_str for keyword in self.error_keywords):
                logger.warning(f"检测到错误文本，准备启动后台重试: '{message_str}'")
                should_retry = True

        if not should_retry and is_truly_empty:
            _llm_response = getattr(result, 'raw', None) if result else None
            is_final_turn = True 

            if _llm_response and hasattr(_llm_response, 'choices') and _llm_response.choices:
                finish_reason = getattr(_llm_response.choices[0], 'finish_reason', 'stop')
                
                if finish_reason == 'tool_calls':
                    is_final_turn = False
                    logger.debug(
                        f"检测到空回复，但LLM结束原因为'{finish_reason}'，属于正常工具调用，跳过重试。"
                    )
            
            if is_final_turn:
                logger.warning("检测到最终回复为空，准备启动后台重试。")
                should_retry = True
        
        if should_retry and event.message_str.strip():
            logger.info("创建后台重试任务，并立即阻止当前错误/空回复的发送。")
            
            prompt_to_retry = event.message_str
            session_id_to_use = event.unified_msg_origin
            images_to_retry = [
                comp.url
                for comp in event.message_obj.message
                if isinstance(comp, Comp.Image) and hasattr(comp, "url") and comp.url
            ]

            # 在触发重试的时刻，捕获当前的工具列表
            provider = self.context.get_using_provider()
            func_tool_to_retry = None
            if provider and hasattr(provider, "func_tool"):
                 func_tool_to_retry = provider.func_tool
            
            # 将捕获到的工具列表传入后台任务
            asyncio.create_task(self._retry_task(
                prompt_to_retry,
                session_id_to_use,
                images_to_retry,
                func_tool_to_retry 
            ))
            
            event.clear_result()
            event.stop_event()

    async def terminate(self):
        logger.info("已卸载 [IntelligentRetry] 插件。")

# --- END OF FILE main.py ---
