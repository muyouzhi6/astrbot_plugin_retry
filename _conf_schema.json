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
    "2.6.1"
)
class IntelligentRetry(Star):
    """
    一个AstrBot插件，在检测到LLM回复为空或返回包含特定关键词的错误文本时，
    自动进行多次重试，并完整保持原有的上下文和人设。
    V2.5.0: 修复了上下文丢失和人设不一致的问题，确保重试时保持完全相同的对话环境。
    V2.6.0: 新增按HTTP状态码决定是否重试的能力（可配置白/黑名单，默认允许400/429/502/503/504）。
    V2.6.1: 新增 always_use_system_prompt 配置，允许在重试时强制覆盖上下文中的 system 消息，统一使用 Provider 的 system_prompt，避免被异常/污染的人设影响。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.max_attempts = config.get('max_attempts', 3)
        self.retry_delay = config.get('retry_delay', 2)
        default_keywords = "api 返回的内容为空\nAPI 返回的 completion 由于内容安全过滤被拒绝(非 AstrBot)\n调用失败"
        keywords_str = config.get('error_keywords', default_keywords)
        self.error_keywords = [k.strip().lower() for k in keywords_str.split('\n') if k.strip()]
        # 是否在重试时强制使用 Provider 的 system_prompt，覆盖上下文中的任意 system 消息
        # 目的：规避极端情况下上下文携带异常/污染的 system 信息导致人格“错乱”
        self.always_use_system_prompt = config.get('always_use_system_prompt', True)

        # 基于状态码的重试控制：
        # - retryable_status_codes    命中这些状态码时允许进入重试。
        # - non_retryable_status_codes 命中这些状态码时直接跳过重试（最高优先级）。
        # 说明：我们是从“错误文本”中解析状态码（4xx/5xx），具体取决于上游 Provider 如何返回错误信息；
        #      若文本里没有状态码，仍会走关键词等其它判定逻辑。
        # 默认将 400 納入可重试集合，以适配“多 Key 轮询”的场景（不同 Key 可能成功）。
        retryable_codes_default = "400\n429\n502\n503\n504"
        non_retryable_codes_default = ""
        retryable_codes_str = config.get('retryable_status_codes', retryable_codes_default)
        non_retryable_codes_str = config.get('non_retryable_status_codes', non_retryable_codes_default)
        def _parse_codes(s: str):
            codes = set()
            for line in s.split('\n'):
                t = line.strip()
                if t.isdigit():
                    try:
                        codes.add(int(t))
                    except Exception:
                        pass
            return codes
        self.retryable_status_codes = _parse_codes(retryable_codes_str)
        self.non_retryable_status_codes = _parse_codes(non_retryable_codes_str)
        # 兜底回复（可选）
        self.fallback_reply = config.get('fallback_reply', "抱歉，刚才遇到服务波动，我已自动为你重试多次仍未成功。请稍后再试或换个说法。")
        
        logger.info(
            f"已加载 [IntelligentRetry] 插件 v2.6.1, "
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
            # 判断上下文中是否已经包含 system 消息（仅用于记录与决策）
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

            # 若开启强制人设，且 Provider 提供了 system_prompt，则移除上下文中的所有 system 消息并强制注入
            if self.always_use_system_prompt:
                if system_prompt:
                    original_len = len(context_history)
                    removed = 0
                    filtered = []
                    for m in context_history:
                        if isinstance(m, dict) and str(m.get('role', '')).lower() == 'system':
                            removed += 1
                            continue
                        filtered.append(m)
                    if removed > 0:
                        logger.debug(f"已强制覆盖人设：移除 {removed} 条历史 system 消息")
                    context_history = filtered
                    # 更新 has_system_in_contexts 标记仅用于后续日志/决策
                    has_system_in_contexts = False
                else:
                    logger.warning("配置了 always_use_system_prompt，但 Provider 未提供 system_prompt，已回退为上下文判断模式")
            
            # 使用完整的参数进行重试
            kwargs = {
                'prompt': event.message_str,
                'contexts': context_history,
                'image_urls': image_urls,
                'func_tool': func_tool,
            }
            # 强制人设：无条件传入 Provider 的 system_prompt
            if self.always_use_system_prompt and system_prompt:
                kwargs['system_prompt'] = system_prompt
            # 非强制：仅当上下文没有 system 消息时，额外传入 Provider 的 system_prompt
            elif not self.always_use_system_prompt and (not has_system_in_contexts) and system_prompt:
                kwargs['system_prompt'] = system_prompt

            llm_response = await provider.text_chat(**kwargs)
            
            return llm_response
            
        except Exception as e:
            logger.error(f"重试调用LLM时发生错误: {e}")
            return None

    def _extract_status_code(self, text: str):
        """
        从错误文本中提取 4xx/5xx 状态码（简易解析），返回 int 或 None。
        注意：
        - 这里并不直接访问 HTTP 响应对象，而是对上游返回的『文本』进行正则解析；
        - 解析到的首个 4xx/5xx 会被认为是本次错误的代表状态码；
        - 若 Provider 的错误格式不同（不含状态码），则返回 None，由其它判定逻辑兜底。
        示例可匹配："HTTP 502 Bad Gateway"、"status: 429"、"... 400 ..."。
        """
        if not text:
            return None
        try:
            import re
            m = re.search(r"\b([45]\d{2})\b", text)
            if m:
                return int(m.group(1))
        except Exception:
            pass
        return None

    def _should_retry(self, result):
        """
        判断是否需要重试：
        判定顺序（从高到低）：
        1) 结果对象为空 或 文本内容为空 -> 重试
        2) 文本中解析到状态码：
           - 命中 non_retryable_status_codes -> 不重试（优先级最高）
           - 命中 retryable_status_codes    -> 重试
        3) 命中错误关键词 -> 重试
        4) 其它情况 -> 不重试
        """
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
        
    # 检查是否包含错误关键词或可重试状态码
        message_str = result.get_plain_text()
        if message_str:
            code = self._extract_status_code(message_str)
            if code is not None:
                if code in self.non_retryable_status_codes:
                    logger.debug(f"检测到状态码 {code}，配置为不可重试，跳过重试")
                    return False
                if code in self.retryable_status_codes:
                    logger.debug(f"检测到状态码 {code}，配置允许重试")
                    return True
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

            # 检查新回复是否包含错误关键词或特定状态码：
            # - 若命中不可重试状态码，提前结束循环；
            # - 若命中可重试状态码，将 has_error 置为 True 继续重试；
            # - 否则按关键词结果决定。
            new_text_lower = new_text.lower()
            has_error = any(keyword in new_text_lower for keyword in self.error_keywords)
            code = self._extract_status_code(new_text)
            if code is not None:
                if code in self.non_retryable_status_codes:
                    logger.warning(f"检测到不可重试状态码 {code}，提前结束重试")
                    break
                if code in self.retryable_status_codes:
                    has_error = True

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
