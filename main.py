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
    "当LLM回复空或返回错误文本时，进行多次重试",
    "2.1.0" # 放弃不存在的钩子，回归基本实现
)
class IntelligentRetry(Star):
    """
    一个AstrBot插件，通过重新构建请求的方式，实现带上下文的重试。
    V2.1.0: 放弃了不存在的 on_before_llm_request 钩子，回归到在 on_decorating_result 中
            重新构建请求并进行重试的可靠模式。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.max_attempts = config.get('max_attempts', 3)
        self.retry_delay = config.get('retry_delay', 2)
        default_keywords = "api 返回的内容为空\n请求失败\n错误类型\n调用失败\n处理失败"
        keywords_str = config.get('error_keywords', default_keywords)
        self.error_keywords = [k.strip().lower() for k in keywords_str.split('\n') if k.strip()]
        
        logger.info(
            f"已加载 [IntelligentRetry] 插件 v2.1.0, "
            f"将在LLM回复无效时自动重试 (最多 {self.max_attempts} 次)。"
        )

    # 这个函数现在是重试的核心，它根据 event 信息重新构建请求
    async def _perform_retry(self, event: AstrMessageEvent):
        """
        根据 event 对象的信息，重新构建一个完整的请求并执行。
        这是确保人设和上下文被保留的关键。
        """
        provider = self.context.get_using_provider()
        if not provider:
            logger.warning("LLM提供商未启用，无法重试。")
            return None
            
        try:
            # 1. 获取图片URL
            image_urls = [
                comp.url
                for comp in event.message_obj.message
                if isinstance(comp, Comp.Image) and hasattr(comp, "url") and comp.url
            ]

            # 2. 获取对话历史
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(
                event.unified_msg_origin
            )
            context_history = []
            if curr_cid:
                conv = await self.context.conversation_manager.get_conversation(
                    event.unified_msg_origin, curr_cid
                )
                if conv and conv.history:
                    # 去掉最后一条（也就是当前用户的输入），因为 prompt 会单独提供
                    full_history = await asyncio.to_thread(json.loads, conv.history)
                    context_history = full_history[:-1] if full_history else []


            # 3. 获取 System Prompt (人设)
            system_prompt = provider.system_prompt if hasattr(provider, "system_prompt") else None
            if not system_prompt and hasattr(provider, "config"):
                 system_prompt = provider.config.get("system_prompt")

            # 4. 获取工具函数列表 (非常重要)
            func_tool = provider.func_tool if hasattr(provider, "func_tool") else None


            logger.debug(f"正在重新构建请求进行重试... Prompt: '{event.message_str}'")
            llm_response = await provider.text_chat(
                prompt=event.message_str,
                contexts=context_history,
                image_urls=image_urls,
                system_prompt=system_prompt,
                func_tool=func_tool # 将工具函数也加回去
            )
            return llm_response
        except Exception as e:
            logger.error(f"重试调用LLM时发生错误: {e}")
            return None

    async def _retry_task(self, event: AstrMessageEvent):
        """独立的后台任务，现在它接收 event 对象"""
        for attempt in range(1, self.max_attempts + 1):
            logger.info(f"后台重试任务: 第 {attempt}/{self.max_attempts} 次尝试...")
            new_response = await self._perform_retry(event) # 传递 event

            new_text = ""
            if new_response and hasattr(new_response, 'completion_text'):
                new_text = new_response.completion_text.strip()
            
            if new_text and not any(keyword in new_text.lower() for keyword in self.error_keywords):
                logger.info("后台重试成功，正在直接发送新回复。")
                await self.context.send_message(new_text, event.unified_msg_origin)
                return

            if attempt < self.max_attempts:
                logger.warning(f"后台重试尝试失败，将在 {self.retry_delay} 秒后进行下一次尝试...")
                await asyncio.sleep(self.retry_delay)
        
        logger.error(f"所有 {self.max_attempts} 次后台重试均失败，放弃该次回复。")

    @filter.on_decorating_result(priority=-1)
    async def check_and_retry(self, event: AstrMessageEvent):
        """
        在唯一可靠的钩子中，检测错误，并启动一个传递 event 对象的后台重试任务。
        """
        if self.max_attempts <= 0:
            return

        result = event.get_result()
        if not result:
            return

        message_str = result.get_plain_text()
        should_retry = False

        if message_str:
            lower_message_str = message_str.lower()
            if any(keyword in lower_message_str for keyword in self.error_keywords):
                logger.warning(f"检测到错误文本，准备启动后台重试: '{message_str}'")
                should_retry = True

        if not should_retry:
            has_content = any(
                (isinstance(c, Comp.Plain) and c.text.strip()) or not isinstance(c, Comp.Plain)
                for c in result.chain
            )
            if not has_content:
                logger.warning("检测到空回复，准备启动后台重试。")
                should_retry = True
        
        # 只有在用户确实发送了文本内容时才进行重试，避免纯图片消息触发不必要的LLM调用
        if should_retry and event.message_str.strip():
            logger.info("创建后台重试任务，并立即阻止当前错误/空回复的发送。")
            # 将完整的 event 对象传递给后台任务
            asyncio.create_task(self._retry_task(event))
            
            event.clear_result()
            event.stop_event()

    async def terminate(self):
        logger.info("已卸载 [IntelligentRetry] 插件。")# --- START OF FILE main.py ---

