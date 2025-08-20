import asyncio
import json
import re
from typing import Optional, Set, List, Dict, Any, Tuple, Union

# AstrBot 运行环境导入；若在本地无框架，使用轻量兼容桩以便导入通过
try:
    from astrbot.api.event import AstrMessageEvent, filter  # type: ignore
    from astrbot.api.star import Context, Star, register  # type: ignore
    from astrbot.api import logger  # type: ignore
    import astrbot.api.message_components as Comp  # type: ignore
except Exception:  # 仅用于本地/测试环境兼容
    class Context: ...

    class Star:
        def __init__(self, context):
            self.context = context

    def register(*_args, **_kwargs):
        def _deco(cls):
            return cls
        return _deco

    class AstrMessageEvent: ...

    class filter:
        @staticmethod
        def on_llm_request(*args, **kwargs):
            def _deco(fn):
                return fn
            return _deco

        @staticmethod
        def on_llm_response(*args, **kwargs):
            def _deco(fn):
                return fn
            return _deco

    class logger:  # 简易日志
        @staticmethod
        def info(msg): print(f"[INFO] {msg}")
        @staticmethod
        def warning(msg): print(f"[WARN] {msg}")
        @staticmethod
        def error(msg): print(f"[ERROR] {msg}")
        @staticmethod
        def debug(msg): print(f"[DEBUG] {msg}")

    class Comp:
        class Image:
            def __init__(self, url=None):
                self.url = url


# 为了兼容静态检查器，提供一个始终存在的基类别名
try:
    BaseStar = Star  # type: ignore[name-defined]
except Exception:  # pragma: no cover - 仅在极端环境下触发
    class BaseStar:
        def __init__(self, context):
            self.context = context


@register(
    "astrabot_plugin_retry",
    "木有知 & 长安某",
    "当LLM回复为空或包含特定错误关键词时，自动进行多次重试，保持完整上下文和人设。激进截断检测v4.4 - 用户可控",
    "4.4.0"
)
class IntelligentRetry(BaseStar):
    """
    智能重试插件：
    - 空回复检测
    - 错误关键词与正则模式检测
    - HTTP 4xx/5xx 状态码检测（禁止 > 允许 > 关键词/截断）
    - 截断检测（finish_reason=length + 文本启发式）
    - 完整上下文与人设控制（可移除历史 system 并使用 Provider/fallback）
    - 多次重试 + 自适应/指数延迟
    - 兜底回复
    """

    def __init__(self, context: Context):
        super().__init__(context)

        # 读取配置
        try:
            config = getattr(context, 'config_helper', None)
            logger.debug(f"[重试插件] config_helper = {config}")
            if config and hasattr(config, 'get_plugin_config'):
                config_data = config.get_plugin_config() or {}
            else:
                config_data = {}
        except Exception as e:
            logger.warning(f"[重试插件] 配置读取异常: {e}")
            config_data = {}
        logger.debug(f"[重试插件] config_data = {config_data}")

        # 配置项
        self.enable_truncation_detection: bool = bool(config_data.get('enable_truncation_detection', True))
        self.enable_error_keyword_detection: bool = bool(config_data.get('enable_error_keyword_detection', True))
        self.adaptive_delay: bool = bool(config_data.get('adaptive_delay', True))
        self.max_attempts: int = int(config_data.get('max_attempts', 3) or 3)

        # retry_delay 允许 float；若 schema 给了 int 也能解析
        _retry_delay_raw = config_data.get('retry_delay', 2)
        try:
            self.retry_delay: float = float(_retry_delay_raw)
        except (TypeError, ValueError):
            self.retry_delay = 2.0 if _retry_delay_raw in (None, "") else 2.0

        # 错误关键词默认
        schema_default_keywords = (
            "api 返回的内容为空\n"
            "API 返回的内容为空\n"
            "APITimeoutError\n"
            "错误类型: Exception\n"
            "API 返回的 completion 由于内容安全过滤被拒绝(非 AstrBot)\n"
            "语音转换失败，请稍后再试\n"
            "语音转换失败\n"
            "网络连接超时\n"
            "服务器暂时不可用\n"
            "请求频率过高\n"
            "连接失败"
        )
        error_keywords_text: str = config_data.get('error_keywords', schema_default_keywords) or ""
        self.error_keywords: List[str] = [
            kw.strip() for kw in error_keywords_text.split('\n') if kw.strip()
        ]

        # 人设控制
        self.always_use_system_prompt: bool = bool(config_data.get('always_use_system_prompt', True))
        self.fallback_system_prompt_text: str = str(config_data.get('fallback_system_prompt', "") or "")

        # 状态码配置（含 524）
        self.retryable_status_codes: Set[int] = self._parse_codes(
            config_data.get('retryable_status_codes', '429\n500\n502\n503\n504\n524')
        )
        self.non_retryable_status_codes: Set[int] = self._parse_codes(
            config_data.get('non_retryable_status_codes', '400\n401\n403\n404')
        )

        # 调试/日志输出配置
    self.log_context_preview: bool = bool(config_data.get('log_context_preview', False))
    self.context_preview_last_n: int = int(config_data.get('context_preview_last_n', 10) or 10)
        self.context_preview_max_chars: int = int(config_data.get('context_preview_max_chars', 120) or 120)

        # 兜底回复
        schema_default_fallback = "抱歉，刚才遇到服务波动，我已自动为你重试多次仍未成功。请稍后再试或换个说法。"
        user_fallback = config_data.get('fallback_reply', None)
        # 明确遵循 schema 提示：留空则不发送消息
        if user_fallback is None:
            self.fallback_reply = schema_default_fallback
        elif str(user_fallback).strip() == "":
            self.fallback_reply = ""  # 显式禁用兜底
        else:
            self.fallback_reply = str(user_fallback)
        logger.info(f"[重试插件] 已加载 v4.4，最多重试 {self.max_attempts} 次，基础延迟 {self.retry_delay}s")
        logger.debug(
            f"[重试插件] 截断检测={'启用' if self.enable_truncation_detection else '禁用'} | 错误检测={'启用' if self.enable_error_keyword_detection else '禁用'}"
        )
        logger.debug(f"[重试插件] 兜底回复预览: '{self.fallback_reply[:30]}...'")

    def _parse_codes(self, codes_str: str) -> Set[int]:
        codes: Set[int] = set()
        for line in (codes_str or '').split('\n'):
            s = (line or '').strip()
            if s.isdigit():
                try:
                    code = int(s)
                    if 400 <= code <= 599:
                        codes.add(code)
                except Exception:
                    continue
        return codes

    async def _get_complete_context(self, unified_msg_origin: Optional[str]) -> List[Dict[str, Any]]:
        if not unified_msg_origin:
            return []
        try:
            if not hasattr(self.context, 'conversation_manager') or not self.context.conversation_manager:
                return []
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(unified_msg_origin)
            if not curr_cid:
                return []
            conv = await self.context.conversation_manager.get_conversation(unified_msg_origin, curr_cid)
            if not conv or not getattr(conv, 'history', None):
                return []
            context_history = json.loads(conv.history)
            return context_history if isinstance(context_history, list) else []
        except Exception as e:
            logger.warning(f"[重试插件] 获取对话上下文失败: {e}")
            return []

    async def _get_provider_config(self) -> Tuple[Optional[Any], Optional[str], Optional[Any]]:
        provider = None
        try:
            provider = self.context.get_using_provider()
        except Exception as e:
            logger.error(f"[重试插件] 获取 Provider 失败: {e}")
        if not provider:
            return None, None, None

        system_prompt = None
        try:
            if hasattr(provider, "system_prompt"):
                system_prompt = provider.system_prompt
            elif hasattr(provider, "config") and provider.config:
                system_prompt = provider.config.get("system_prompt")
        except Exception:
            pass

        func_tool = None
        try:
            if hasattr(provider, "func_tool"):
                func_tool = provider.func_tool
        except Exception:
            pass

        return provider, system_prompt, func_tool

    def _extract_context_system_info(self, context_history: List[Dict[str, Any]]) -> Tuple[bool, str]:
        has_system = False
        sys_preview = ""
        try:
            for msg in context_history:
                if isinstance(msg, dict) and str(msg.get('role', '')).lower() == 'system':
                    has_system = True
                    content = msg.get('content', '')
                    sys_preview = str(content)[:60] if content else ""
                    break
        except Exception:
            pass
        return has_system, sys_preview

    def _filter_system_messages(self, context_history: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
        filtered: List[Dict[str, Any]] = []
        removed = 0
        for msg in context_history:
            if isinstance(msg, dict) and str(msg.get('role', '')).lower() == 'system':
                removed += 1
            else:
                filtered.append(msg)
        return filtered, removed

    def _set_fallback_response(self, response) -> None:
        # 若用户显式禁用了兜底，直接返回
        if not getattr(self, 'fallback_reply', None):
            logger.info("[重试插件] 用户已禁用兜底回复，跳过替换")
            return
        try:
            try:
                from astrbot.api.message_components import Plain  # type: ignore
            except Exception as import_error:
                logger.warning(f"[重试插件] Plain 组件导入失败: {import_error}")
                class Plain:  # 兼容模式
                    def __init__(self, text):
                        self.text = text
                        self.type = 'Plain'
                        self.convert = True
            fallback_plain = Plain(text=self.fallback_reply)

            if hasattr(response, 'result_chain') and hasattr(response.result_chain, 'chain'):
                response.result_chain.chain = [fallback_plain]
                logger.info("[重试插件] 兜底回复设置成功(模式1)")
            elif hasattr(response, 'result_chain'):
                fallback_chain = type('MessageChain', (), {'chain': [fallback_plain]})()
                response.result_chain = fallback_chain
                logger.info("[重试插件] 兜底回复设置成功(模式2)")
            else:
                logger.warning("[重试插件] 无法设置兜底回复：response 格式不支持")
        except Exception as e:
            logger.error(f"[重试插件] 设置兜底回复失败: {e}")

    async def _perform_retry_with_context(self, event: Any) -> Optional[Any]:
        provider, system_prompt, func_tool = await self._get_provider_config()
        if not provider:
            logger.error("[重试插件] LLM 提供商不可用，无法重试")
            return None
        try:
            context_history = await self._get_complete_context(getattr(event, 'unified_msg_origin', None))
            has_system_in_contexts, _ = self._extract_context_system_info(context_history)

            image_urls: List[str] = []
            try:
                msg_obj = getattr(event, 'message_obj', None)
                if msg_obj and hasattr(msg_obj, 'message'):
                    for comp in msg_obj.message:
                        if hasattr(comp, 'url') and comp.url:
                            image_urls.append(comp.url)
            except Exception:
                pass

            logger.info(f"[重试插件] 正在重试... 上下文长度: {len(context_history)}")

            if self.log_context_preview and context_history and self.context_preview_last_n > 0:
                try:
                    tail = context_history[-self.context_preview_last_n:]
                    lines: List[str] = []
                    for idx, m in enumerate(tail, 1):
                        if isinstance(m, dict):
                            role = str(m.get('role', ''))
                            content = m.get('content', '')
                        else:
                            role = ''
                            content = str(m)
                        text = str(content).replace('\n', ' ')
                        if len(text) > self.context_preview_max_chars:
                            text = text[:self.context_preview_max_chars] + '…'
                        lines.append(f"#{idx} [{role}] {text}")
                    logger.debug("[重试插件] 上下文预览\n" + "\n".join(lines))
                except Exception:
                    pass

            if self.always_use_system_prompt:
                if not system_prompt and self.fallback_system_prompt_text:
                    system_prompt = self.fallback_system_prompt_text
                    logger.info("[重试插件] 使用备用人设")
                if system_prompt:
                    context_history, removed = self._filter_system_messages(context_history)
                    if removed > 0:
                        logger.info(f"[重试插件] 移除 {removed} 条历史 system 消息以强制覆盖人设")
                    has_system_in_contexts = False

            kwargs: Dict[str, Any] = {
                'prompt': getattr(event, 'message_str', ''),
                'contexts': context_history,
                'image_urls': image_urls,
                'func_tool': func_tool,
            }
            if self.always_use_system_prompt and system_prompt:
                kwargs['system_prompt'] = system_prompt
            elif (not self.always_use_system_prompt) and (not has_system_in_contexts) and system_prompt:
                kwargs['system_prompt'] = system_prompt

            llm_response = await provider.text_chat(**kwargs)
            return llm_response
        except Exception as e:
            logger.error(f"[重试插件] 重试调用 LLM 失败: {e}")
            return None

    def _detect_truncation(self, text: str, llm_response=None) -> bool:
        if not text:
            return True
        if llm_response:
            try:
                if hasattr(llm_response, 'choices') and getattr(llm_response, 'choices'):
                    finish_reason = getattr(llm_response.choices[0], 'finish_reason', None)
                    if finish_reason == 'length':
                        logger.info("[重试插件] 检测到 finish_reason='length'，官方确认截断")
                        return True
                elif isinstance(llm_response, dict):
                    choices = llm_response.get('choices') or []
                    if isinstance(choices, list) and choices:
                        fr = choices[0].get('finish_reason')
                        if fr == 'length':
                            logger.info("[重试插件] 检测到 finish_reason='length' (dict)，官方确认截断")
                            return True
            except Exception:
                pass

        text = text.strip()
        if re.search(r'\d+\.\s*$', text):
            return True
        if re.search(r'[（(]\d+[）)]\s*$', text):
            return True
        return not self._is_clearly_complete(text)

    def _is_clearly_complete(self, text: str) -> bool:
        if not text or not text.strip():
            return False
        text = text.strip()

        if text.endswith(('.', '。', '！', '!', '？', '?')):
            return True
        if text.endswith(('…', ';', '；')):
            return True
        if text.endswith(('"', '“', '”', "'")):
            return True
        if text.endswith((')', '）', ']', '】', '}', '》')):
            return True
        if text.endswith('```'):
            return True
        if re.search(r'\.(com|org|net|edu|gov|cn|co\.uk|json|txt|py|js|html|css|md|pdf|docx?|xlsx?)$', text):
            return True
        if re.search(r'v?\d+\.\d+(\.\d+)?$', text):
            return True
        if re.search(r'\d+%$', text):
            return True
        if re.search(r'\d+(\.\d+)?\s*(GB|MB|KB|TB|元|块|个|次|秒|分钟|小时|天|年|月|kg|g|m|cm|km)$', text):
            return True

        completion_words = ['完成', '结束', '搞定', '好的', '明白', '了解', '收到', '明白了', '知道了', '完成了', '结束了', 'done', 'finished', 'complete', 'ok', 'got it']
        for word in completion_words:
            if text.endswith(word):
                return True

        flexible_completion_patterns = [
            r'完成了?\b',
            r'明白了?\b',
            r'知道了?\b',
            r'了解了?\b',
            r'收到了?\b',
            r'好的\b',
            r'搞定了?\b'
        ]
        for pattern in flexible_completion_patterns:
            if re.search(pattern, text) and len(text) <= 10:
                return True

        words = re.findall(r'[\u4e00-\u9fff]+|[a-zA-Z]+', text)
        if len(words) >= 3:
            last_word = words[-1] if words else ""
            if len(last_word) >= 1 and last_word.lower() not in [
                '但是', '然后', '所以', '而且', '另外', '因此', '于是', '接着', '包括', '如下', '还有', '以及',
                'however', 'therefore', 'moreover', 'furthermore', 'because', 'since', 'including'
            ]:
                completeness_indicators = [
                    '是', '有', '会', '能', '可以', '应该', '需要', '正常', '成功', '完整', '回复', '内容',
                    '没有', '问题', '这是', '这个', '一个', '已经', '现在', '可能', '应该', '确实',
                    '知道', '明白', '理解', '认为', '觉得', '发现', '注意', '建议', '推荐', '希望'
                ]
                has_completeness = any(p in text for p in completeness_indicators)
                is_reasonable_length = len(text) >= 6
                if has_completeness and is_reasonable_length:
                    return True

        complete_patterns = [
            r'^正常的.{2,}$',
            r'^这是.{4,}[^一个的]$',
            r'^我.{2,}了$',
            r'^.{3,}内容$',
            r'^.{3,}没有问题$',
            r'^.{2,}很好$',
            r'^.{2,}不错$',
        ]
        for pattern in complete_patterns:
            if re.match(pattern, text):
                return True
        return False

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response) -> bool:
        try:
            if not response:
                return True

            reply_text = ""
            if hasattr(response, 'result_chain') and getattr(response, 'result_chain'):
                try:
                    for comp in response.result_chain.chain:
                        if hasattr(comp, 'text') and comp.text:
                            reply_text += comp.text
                except Exception:
                    pass

            raw_completion = getattr(response, 'raw_completion', None)

            should_retry = self._should_retry_simple(reply_text, raw_completion)

            if should_retry:
                logger.info(f"[重试插件] 检测到无效回复，准备重试: '{reply_text[:50]}...'")

                for attempt in range(1, self.max_attempts + 1):
                    logger.info(f"[重试插件] 第 {attempt}/{self.max_attempts} 次重试...")

                    if attempt > 1:
                        if self.adaptive_delay:
                            base = max(self.retry_delay, 0.2)
                            if "timeout" in reply_text.lower() or "超时" in reply_text:
                                delay = max(base, 0.2)
                            elif "请求失败" in reply_text or "频率过高" in reply_text or "429" in reply_text:
                                delay = max(base, 0.5)
                            else:
                                delay = max(base, 0.5)
                            delay = min(delay + 0.1 * (attempt - 1), 3.0)
                        else:
                            base = max(self.retry_delay, 0.2)
                            delay = min(base * (2 ** (attempt - 2)), 6.0)
                        logger.info(f"[重试插件] 延迟 {delay:.2f}s 后重试...")
                        await asyncio.sleep(delay)

                    retry_result = await self._perform_retry_with_context(event)

                    if not retry_result:
                        logger.warning(f"[重试插件] 第 {attempt} 次重试调用失败")
                        continue

                    retry_text = ""
                    if hasattr(retry_result, 'result_chain') and getattr(retry_result, 'result_chain'):
                        try:
                            for comp in retry_result.result_chain.chain:
                                if hasattr(comp, 'text') and comp.text:
                                    retry_text += comp.text
                        except Exception:
                            pass

                    retry_raw_completion = getattr(retry_result, 'raw_completion', None)
                    is_retry_still_invalid = self._should_retry_simple(retry_text, retry_raw_completion)

                    if is_retry_still_invalid:
                        logger.info(f"[重试插件] 第 {attempt} 次重试仍然无效: '{retry_text[:50]}...' (长度:{len(retry_text)})")
                        if attempt < self.max_attempts:
                            continue
                        else:
                            logger.warning(f"[重试插件] 已达到最大重试次数 ({self.max_attempts})，全部重试失败")
                            self._set_fallback_response(response)
                            break
                    else:
                        logger.info(f"[重试插件] 第 {attempt} 次重试成功: '{retry_text[:50]}...' (长度:{len(retry_text)})")
                        if hasattr(retry_result, 'result_chain'):
                            response.result_chain = retry_result.result_chain
                        if hasattr(retry_result, 'raw_completion'):
                            response.raw_completion = retry_result.raw_completion
                        break

            return True
        except Exception as e:
            logger.error(f"[重试插件] 处理响应时发生错误: {e}")
            return True

    def _should_retry_simple(self, text: str, llm_response: Optional[Union[dict, Any]] = None) -> bool:
        # 0. 工具调用/非文本完成保护
        try:
            finish_reason = None
            if llm_response is not None:
                if isinstance(llm_response, dict):
                    choices = llm_response.get('choices') or []
                    if isinstance(choices, list) and choices:
                        finish_reason = choices[0].get('finish_reason')
                else:
                    if hasattr(llm_response, 'choices') and getattr(llm_response, 'choices'):
                        first = llm_response.choices[0]
                        finish_reason = getattr(first, 'finish_reason', None)
            if finish_reason in {"tool_calls", "function_call", "content_filter"}:
                logger.debug(f"[重试插件] 检测到非文本完成 finish_reason={finish_reason}，跳过重试")
                return False
        except Exception:
            pass

        # 1. 空回复
        if not text or not text.strip():
            logger.debug("[重试插件] 检测到空回复")
            return True

        # 2. HTTP 状态码优先级（禁止 > 允许）
        try:
            code = self._extract_http_status(text, llm_response)
            if code is not None:
                if code in self.non_retryable_status_codes:
                    logger.info(f"[重试插件] 检测到禁止重试状态码 {code}，跳过重试")
                    return False
                if code in self.retryable_status_codes:
                    logger.info(f"[重试插件] 检测到可重试状态码 {code}，将进行重试")
                    return True
        except Exception:
            pass

        # 3. 错误关键词/模式
        if self.enable_error_keyword_detection:
            text_lower = text.lower().strip()

            specific_errors = [
                "请求失败。错误类型",
                "APTL错误信息",
                "request time out请在控制台查看",
                "分享错误详情",
            ]
            for error in specific_errors:
                if error.lower() in text_lower:
                    logger.debug(f"[重试插件] 检测到特定错误模式: {error}")
                    return True

            error_patterns = [
                r'错误类型.*?exception',
                r'请求.*?失败',
                r'time.*?out',
                r'错误.*?详情',
                r'控制台.*?查看',
            ]
            for pattern in error_patterns:
                if re.search(pattern, text_lower):
                    logger.debug(f"[重试插件] 检测到错误模式: {pattern}")
                    return True

            for keyword in self.error_keywords:
                if keyword.lower() in text_lower:
                    logger.debug(f"[重试插件] 检测到错误关键词: {keyword}")
                    return True

        # 4. 截断检测
        if self.enable_truncation_detection:
            if self._detect_truncation(text, llm_response):
                logger.debug("[重试插件] 检测到回复截断")
                return True
        else:
            logger.debug("[重试插件] 截断检测已禁用，跳过")

        return False

    @staticmethod
    def _extract_http_status(text: str, llm_response: Optional[Union[dict, Any]]) -> Optional[int]:
        # 文本中搜索
        try:
            m = re.search(r"\b([45]\d{2})\b", text or "")
            if m:
                code = int(m.group(1))
                if 400 <= code <= 599:
                    return code
        except Exception:
            pass

        # raw completion 中搜索
        try:
            if llm_response is None:
                return None
            if isinstance(llm_response, dict):
                err = llm_response.get('error') or {}
                for key in ('status', 'code', 'http_status'):
                    val = err.get(key)
                    if isinstance(val, int) and 400 <= val <= 599:
                        return val
                    if isinstance(val, str) and val.isdigit():
                        iv = int(val)
                        if 400 <= iv <= 599:
                            return iv
            else:
                err = getattr(llm_response, 'error', None)
                for key in ('status', 'code', 'http_status'):
                    if err is not None and hasattr(err, key):
                        val = getattr(err, key)
                        try:
                            iv = int(val)
                            if 400 <= iv <= 599:
                                return iv
                        except Exception:
                            pass
        except Exception:
            pass
        return None
