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
    "1.0.0"
)
class IntelligentRetry(Star):
    """
    一个AstrBot插件，根据error_pro插件的逻辑重构。
    在检测到LLM回复为空，或返回包含特定关键词的错误文本时，
    自动进行多次重试。
    """

    # 接收 context 和 config
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        
        # 从配置中读取参数，如果配置不存在则使用默认值
        self.max_attempts = config.get('max_attempts', 3)
        self.retry_delay = config.get('retry_delay', 2)
        
        # 从 text 类型的配置中读取关键词列表
        default_keywords = "api 返回的内容为空\n请求失败\n错误类型\n错误信息\n调用失败\n处理失败"
        keywords_str = config.get('error_keywords', default_keywords)
        self.error_keywords = [k.strip().lower() for k in keywords_str.split('\n') if k.strip()]

        logger.info(
            f"已加载 [IntelligentRetry] 插件, "
            f"将在LLM回复无效时自动重试 (最多 {self.max_attempts} 次)。"
        )
        if not self.error_keywords:
            logger.warning("[IntelligentRetry] 未配置任何错误关键词。")

    async def _perform_retry(self, event: AstrMessageEvent):
        """执行单次重试的核心逻辑"""
        provider = self.context.get_using_provider()
        if not provider:
            logger.warning("LLM提供商未启用，无法重试。")
            return None

        try:
            image_urls = [
                comp.url
                for comp in event.message_obj.message
                if isinstance(comp, Comp.Image) and hasattr(comp, "url") and comp.url
            ]
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(
                event.unified_msg_origin
            )
            context_history = []
            if curr_cid:
                conv = await self.context.conversation_manager.get_conversation(
                    event.unified_msg_origin, curr_cid
                )
                if conv and conv.history:
                    context_history = await asyncio.to_thread(json.loads, conv.history)

            logger.debug(f"正在使用 prompt: '{event.message_str}' 进行重试...")
            llm_response = await provider.text_chat(
                prompt=event.message_str,
                contexts=context_history,
                image_urls=image_urls,
            )
            return llm_response
        except Exception as e:
            logger.error(f"重试调用LLM时发生错误: {e}")
            return None

    @filter.on_decorating_result(priority=-1) # 设置较低优先级，确保在其他插件之后检查最终结果
    async def check_and_retry(self, event: AstrMessageEvent):
        """
        在唯一可靠的钩子中，同时处理“空回复”和“错误文本”两种情况。
        """
        # 如果已禁用重试，则直接返回
        if self.max_attempts <= 0:
            return

        result = event.get_result()
        if not result:
            # 如果一开始就没有结果（可能被其他插件拦截），则不处理
            return

        message_str = result.get_plain_text()
        should_retry = False

        # 检查是否包含错误关键词
        if message_str:
            lower_message_str = message_str.lower()
            if any(keyword in lower_message_str for keyword in self.error_keywords):
                logger.warning(f"检测到错误文本，准备重试: '{message_str}'")
                should_retry = True

        # 如果未命中关键词，再检查是否为空回复
        if not should_retry:
            has_content = any(
                (isinstance(c, Comp.Plain) and c.text.strip())
                or not isinstance(c, Comp.Plain) # 任何非Plain类型的消息段都算作有内容
                for c in result.chain
            )
            if not has_content:
                logger.warning("检测到空回复，准备重试。")
                should_retry = True
        
        # 只有在用户确实发送了文本内容时才进行重试，避免纯图片消息触发不必要的LLM调用
        if should_retry and event.message_str.strip():
            for attempt in range(1, self.max_attempts + 1):
                logger.info(f"第 {attempt}/{self.max_attempts} 次尝试...")
                new_response = await self._perform_retry(event)

                # 检查新回复是否有效且不包含错误关键词
                new_text = new_response.completion_text.strip() if new_response and new_response.completion_text else ""
                if new_text and not any(keyword in new_text.lower() for keyword in self.error_keywords):
                    logger.info("重试成功，已生成新的回复。")
                    event.set_result(event.plain_result(new_text))
                    # 此处不需要 stop_event，因为我们只是替换了结果，事件流程应继续
                    return

                if attempt < self.max_attempts:
                    logger.warning(f"尝试失败，将在 {self.retry_delay} 秒后进行下一次尝试...")
                    await asyncio.sleep(self.retry_delay)

            logger.error(f"所有 {self.max_attempts} 次重试均失败，将清空结果以屏蔽错误。")
            event.clear_result() # 使用 clear_result 更标准
            event.stop_event() # 此时需要停止事件，防止后续流程发送空消息

    async def terminate(self):
        logger.info("已卸载 [IntelligentRetry] 插件。")
