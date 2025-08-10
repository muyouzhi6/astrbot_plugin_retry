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
    "当LLM回复为空或包含特定错误关键词时，自动进行多次重试，保持完整上下文和人设",
    "2.5.0"
)
class IntelligentRetry(Star):
    """
    一个AstrBot插件，在检测到LLM回复为空或返回包含特定关键词的错误文本时，
    自动进行多次重试，并完整保持原有的上下文和人设。
    V2.5.0: 修复了上下文丢失和人设不一致的问题，确保重试时保持完全相同的对话环境。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.max_attempts = config.get('max_attempts', 3)
        self.retry_delay = config.get('retry_delay', 2)
        default_keywords = "api 返回的内容为空\nAPI 返回的 completion 由于内容安全过滤被拒绝(非 AstrBot)\n调用失败"
        keywords_str = config.get('error_keywords', default_keywords)
        self.error_keywords = [k.strip().lower() for k in keywords_str.split('\n') if k.strip()]
        # 兜底回复（可选）
        self.fallback_reply = config.get('fallback_reply', "抱歉，刚才遇到服务波动，我已自动为你重试多次仍未成功。请稍后再试或换个说法。")
        
        logger.info(
            f"已加载 [IntelligentRetry] 插件 v2.5.0, "
            f"将在LLM回复无效时自动重试 (最多 {self.max_attempts} 次)，保持完整上下文和人设。"
        )

    async def _get_complete_context(self, unified_msg_origin: str):
        """获取完整的对话上下文，包括当前消息"""
        try:
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(unified_msg_origin)
            
            if not curr_cid:
                return []
            
            conv = await self.context.conversation_manager.get_conversation(unified_msg_origin, curr_cid)
            
            if not conv or not conv.history:
                return []
            
            # 获取完整的历史对话，包括当前这次对话
            context_history = await asyncio.to_thread(json.loads, conv.history)
            return context_history
            
        except Exception as e:
            logger.error(f"获取对话上下文时发生错误: {e}")
            return []

    async def _get_provider_config(self):
        """获取 LLM 提供商的完整配置，包括人设"""
        provider = self.context.get_using_provider()
        if not provider:
            return None, None, None
        
        # 获取系统提示词（人设）
        system_prompt = None
        if hasattr(provider, "system_prompt"):
            system_prompt = provider.system_prompt
        elif hasattr(provider, "config") and provider.config:
            system_prompt = provider.config.get("system_prompt")
        
        # 获取工具配置
        func_tool = None
        if hasattr(provider, "func_tool"):
            func_tool = provider.func_tool
        
        return provider, system_prompt, func_tool

    async def _perform_retry_with_context(self, event: AstrMessageEvent):
        """执行重试，完整保持原有上下文和人设"""
        provider, system_prompt, func_tool = await self._get_provider_config()
        
        if not provider:
            logger.warning("LLM提供商未启用，无法重试。")
            return None

        try:
            # 获取完整的对话上下文
            context_history = await self._get_complete_context(event.unified_msg_origin)
            # 判断上下文中是否已经包含 system 消息
            has_system_in_contexts = False
            sys_preview = ""
            try:
                for m in context_history:
                    if isinstance(m, dict) and str(m.get('role', '')).lower() == 'system':
                        has_system_in_contexts = True
                        sys_preview = str(m.get('content', ''))[:60]
                        break
            except Exception:
                pass
            
            # 获取图片URL
            image_urls = [
                comp.url
                for comp in event.message_obj.message
                if isinstance(comp, Comp.Image) and hasattr(comp, "url") and comp.url
            ]

            logger.debug(f"正在使用完整上下文进行重试... Prompt: '{event.message_str}'")
            logger.debug(
                f"上下文长度: {len(context_history)}, 系统提示词存在: {system_prompt is not None}, "
                f"上下文含system: {has_system_in_contexts}{'，示例: '+sys_preview if has_system_in_contexts and sys_preview else ''}"
            )
            
            # 使用完整的参数进行重试
            kwargs = {
                'prompt': event.message_str,
                'contexts': context_history,
                'image_urls': image_urls,
                'func_tool': func_tool,
            }
            # 仅当上下文没有 system 消息时，才额外传递 provider 的 system_prompt
            if not has_system_in_contexts and system_prompt:
                kwargs['system_prompt'] = system_prompt

            llm_response = await provider.text_chat(**kwargs)
            
            return llm_response
            
        except Exception as e:
            logger.error(f"重试调用LLM时发生错误: {e}")
            return None

    def _should_retry(self, result):
        """判断是否需要重试"""
        if not result:
            logger.debug("结果为空，需要重试")
            return True
        
        # 检查是否有实际内容
        has_content = any(
            (isinstance(c, Comp.Plain) and c.text.strip())
            or not isinstance(c, Comp.Plain)  # 任何非Plain类型的消息段都算作有内容
            for c in result.chain
        )
        
        if not has_content:
            logger.debug("检测到空回复，需要重试")
            return True
        
        # 检查是否包含错误关键词
        message_str = result.get_plain_text()
        if message_str:
            lower_message_str = message_str.lower()
            for keyword in self.error_keywords:
                if keyword in lower_message_str:
                    logger.debug(f"检测到错误关键词 '{keyword}'，需要重试")
                    return True
        
        return False

    @filter.on_decorating_result(priority=-1)
    async def check_and_retry(self, event: AstrMessageEvent):
        """
        检查结果并进行重试，保持完整的上下文和人设
        """
        # 如果禁用重试则直接返回
        if self.max_attempts <= 0:
            return

        # 检查原始LLM响应，如果是工具调用则不干预
        _llm_response = getattr(event, 'llm_response', None)
        if _llm_response and hasattr(_llm_response, 'choices') and _llm_response.choices:
            finish_reason = getattr(_llm_response.choices[0], 'finish_reason', None)
            if finish_reason == 'tool_calls':
                logger.debug("检测到正常的工具调用，不进行干预")
                return

        result = event.get_result()
        
        # 检查是否需要重试
        if not self._should_retry(result):
            return
        
        # 只有在用户发送了文本内容时才进行重试
        if not event.message_str or not event.message_str.strip():
            logger.debug("用户消息为空，跳过重试")
            return

        logger.info("检测到需要重试的情况，开始重试流程")

        # 进行重试（带指数退避）
        delay = max(0, int(self.retry_delay))
        for attempt in range(1, self.max_attempts + 1):
            logger.info(f"第 {attempt}/{self.max_attempts} 次重试...")

            new_response = await self._perform_retry_with_context(event)

            if not new_response or not getattr(new_response, 'completion_text', ''):
                logger.warning(f"第 {attempt} 次重试返回空结果")
                if attempt < self.max_attempts and delay > 0:
                    await asyncio.sleep(delay)
                    # 指数退避，最高不超过 30 秒
                    delay = min(delay * 2, 30)
                continue

            new_text = new_response.completion_text.strip()

            # 检查新回复是否包含错误关键词
            new_text_lower = new_text.lower()
            has_error = any(keyword in new_text_lower for keyword in self.error_keywords)

            if new_text and not has_error:
                logger.info(f"第 {attempt} 次重试成功，生成有效回复")
                # 直接替换结果，保持事件流程的完整性
                event.set_result(event.plain_result(new_text))
                return
            else:
                logger.warning(f"第 {attempt} 次重试仍包含错误或为空: {new_text[:100]}...")
                if attempt < self.max_attempts and delay > 0:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)

        logger.error(f"所有 {self.max_attempts} 次重试均失败")
        # 若配置了兜底回复，则发送友好提示；否则维持清空并停止
        if self.fallback_reply and self.fallback_reply.strip():
            event.set_result(event.plain_result(self.fallback_reply.strip()))
        else:
            event.clear_result()
            event.stop_event()

    async def terminate(self):
        logger.info("已卸载 [IntelligentRetry] 插件。")

# --- END OF FILE main.py ---
