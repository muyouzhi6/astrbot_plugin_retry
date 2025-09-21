# --- START OF FILE main.py ---

import asyncio
import json
import re
from typing import Dict, Any, Optional

import astrbot.api.message_components as Comp
from astrbot.api import logger, AstrBotConfig
from astrbot.api.star import Context, Star, register
from astrbot.api.event import AstrMessageEvent, filter


@register(
    "intelligent_retry",
    "木有知 & 长安某 (优化增强版)",
    "当LLM回复为空或包含特定错误关键词时，自动进行多次重试，使用原始请求参数确保完整重试。新增智能截断检测与并发重试功能，简化架构提升性能。",
    "2.9.9",
)
class IntelligentRetry(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)

        # 请求存储（借鉴v2版本的设计）
        self.pending_requests: Dict[str, Dict[str, Any]] = {}

        # 解析配置
        self._parse_config(config)

        logger.info(
            f"已加载 [IntelligentRetry] 插件 v2.9.8 , "
            f"将在LLM回复无效时自动重试 (最多 {self.max_attempts} 次)，使用原始请求参数确保完整的重试。"
            f"截断检测模式: {self.truncation_detection_mode}, 并发重试: {'启用' if self.enable_concurrent_retry else '禁用'}"
        )

    def _parse_config(self, config: AstrBotConfig) -> None:
        """解析配置文件，统一配置初始化逻辑"""
        # 基础配置
        self.max_attempts = config.get("max_attempts", 3)
        self.retry_delay = config.get("retry_delay", 2)
        self.retry_delay_mode = (
            config.get("retry_delay_mode", "exponential").lower().strip()
        )

        # 错误关键词配置
        default_keywords = (
            "api 返回的内容为空\n"
            "API 返回的 completion 由于内容安全过滤被拒绝(非 AstrBot)\n"
            "调用失败\n"
            "[TRUNCATED_BY_LENGTH]\n"
            "达到最大长度限制而被截断"
        )
        keywords_str = config.get("error_keywords", default_keywords)
        self.error_keywords = [
            k.strip().lower() for k in keywords_str.split("\n") if k.strip()
        ]

        # 基于状态码的重试控制
        self.retryable_status_codes = self._parse_status_codes(
            config.get("retryable_status_codes", "400\n429\n502\n503\n504")
        )
        self.non_retryable_status_codes = self._parse_status_codes(
            config.get("non_retryable_status_codes", "")
        )

        # 兜底回复
        self.fallback_reply = config.get(
            "fallback_reply",
            "抱歉，刚才遇到服务波动，我已自动为你重试多次仍未成功。请稍后再试或换个说法。",
        )

        # 截断重试配置
        self.enable_truncation_retry = bool(
            config.get("enable_truncation_retry", False)
        )

        # 新增：截断检测模式和选项
        self.truncation_detection_mode = (
            config.get("truncation_detection_mode", "enhanced").lower().strip()
        )
        self.check_structural_integrity = bool(
            config.get("check_structural_integrity", True)
        )
        self.check_content_type_specific = bool(
            config.get("check_content_type_specific", True)
        )
        self.min_reasonable_length = max(
            5, int(config.get("min_reasonable_length", 10))
        )
        self.code_block_detection = bool(config.get("code_block_detection", True))
        self.quote_matching_detection = bool(
            config.get("quote_matching_detection", True)
        )

        # 原有的正则表达式配置（保持向后兼容）
        self.truncation_valid_tail_pattern = config.get(
            "truncation_valid_tail_pattern",
            r"[。！？!?,;:、，．…—\-\(\)\[\]'\""
            "''\\w\\d_\u4e00-\u9fa5\\s\\t]$"
            r"|\.(com|cn|org|net|io|ai|pdf|jpg|png|jpeg|gif|mp3|mp4|txt|zip|tar|gz|html|htm)$"
            r"|https?://[\\w\.-]+$",
        )

        # 并发重试配置 - 遵循官方性能和安全规范
        self.enable_concurrent_retry = bool(
            config.get("enable_concurrent_retry", False)
        )
        self.concurrent_retry_threshold = max(
            0, int(config.get("concurrent_retry_threshold", 1))
        )

        # 基础并发数量配置
        concurrent_count = int(config.get("concurrent_retry_count", 2))
        self.concurrent_retry_count = max(
            1, min(concurrent_count, 5)
        )  # 基础并发数1-5范围

        # 指数增长控制配置
        self.enable_exponential_growth = bool(
            config.get("enable_exponential_growth", True)
        )
        self.max_concurrent_multiplier = max(
            2, min(int(config.get("max_concurrent_multiplier", 4)), 8)
        )
        self.absolute_concurrent_limit = max(
            5, min(int(config.get("absolute_concurrent_limit", 10)), 20)
        )

        # 超时时间限制，遵循官方资源管理规范
        timeout = int(config.get("concurrent_retry_timeout", 30))
        self.concurrent_retry_timeout = max(5, min(timeout, 300))  # 5-300秒范围

        # 配置验证日志 - 使用官方logger规范
        if self.enable_concurrent_retry:
            max_concurrent = min(
                self.concurrent_retry_count * self.max_concurrent_multiplier,
                self.absolute_concurrent_limit,
            )
            logger.info(
                f"并发重试配置: 阈值={self.concurrent_retry_threshold}(0=立即并发), "
                f"基础并发数={self.concurrent_retry_count}, 最大并发={max_concurrent}, "
                f"超时={self.concurrent_retry_timeout}s, 指数增长={'启用' if self.enable_exponential_growth else '禁用'}"
            )

    def _parse_status_codes(self, codes_str: str) -> set:
        """解析状态码配置字符串"""
        codes = set()
        for line in codes_str.split("\n"):
            line = line.strip()
            if line.isdigit():
                try:
                    codes.add(int(line))
                except Exception:
                    pass
        return codes

    def _get_request_key(self, event: AstrMessageEvent) -> str:
        """生成稳定的请求唯一标识符，遵循AstrBot事件处理规范"""
        import hashlib

        # 使用AstrBot官方推荐的事件属性组合
        message_id = getattr(event.message_obj, "message_id", "")
        timestamp = getattr(event.message_obj, "timestamp", 0)
        session_info = event.unified_msg_origin  # 官方推荐的会话标识

        # 生成稳定的内容hash
        content_hash = hashlib.md5(
            f"{event.message_str}{timestamp}".encode()
        ).hexdigest()[:8]

        return f"{session_info}_{message_id}_{content_hash}"

    @filter.on_llm_request()
    async def store_llm_request(self, event: AstrMessageEvent, req):
        """存储LLM请求参数（借鉴v2版本的双钩子机制）"""
        # 检查类型 - 使用鸭子类型检查而不是isinstance以避免导入问题
        if not hasattr(req, "prompt") or not hasattr(req, "contexts"):
            logger.warning(
                "store_llm_request: Expected ProviderRequest-like object but got different type"
            )
            return
        request_key = self._get_request_key(event)

        # 获取图片URL
        image_urls = [
            comp.url
            for comp in event.message_obj.message
            if isinstance(comp, Comp.Image) and hasattr(comp, "url") and comp.url
        ]

        # 存储请求参数 - 注意：此时system_prompt已包含完整的人格信息
        stored_params = {
            "prompt": req.prompt,
            "contexts": getattr(req, "contexts", []),
            "image_urls": image_urls,
            "system_prompt": getattr(req, "system_prompt", ""),
            "func_tool": getattr(req, "func_tool", None),
            "unified_msg_origin": event.unified_msg_origin,
            "conversation": getattr(req, "conversation", None),
        }
        
        # 显式存储sender信息（第一处修改：存储阶段）
        stored_params["sender"] = {
            "user_id": getattr(event.message_obj, "user_id", None),
            "nickname": getattr(event.message_obj, "nickname", None),
            "group_id": getattr(event.message_obj, "group_id", None),
            "platform": getattr(event.message_obj, "platform", None),
            # 如果有其他sender相关字段也可以在这里添加
        }
        
        # 新增：存储Provider的特定参数（model, temperature, max_tokens等）
        # 这些参数对于保证重试的一致性至关重要
        provider_params = {}
        
        # 提取常见的Provider参数
        if hasattr(req, "model"):
            provider_params["model"] = getattr(req, "model", None)
        if hasattr(req, "temperature"):
            provider_params["temperature"] = getattr(req, "temperature", None)
        if hasattr(req, "max_tokens"):
            provider_params["max_tokens"] = getattr(req, "max_tokens", None)
        if hasattr(req, "top_p"):
            provider_params["top_p"] = getattr(req, "top_p", None)
        if hasattr(req, "top_k"):
            provider_params["top_k"] = getattr(req, "top_k", None)
        if hasattr(req, "frequency_penalty"):
            provider_params["frequency_penalty"] = getattr(req, "frequency_penalty", None)
        if hasattr(req, "presence_penalty"):
            provider_params["presence_penalty"] = getattr(req, "presence_penalty", None)
        if hasattr(req, "stop"):
            provider_params["stop"] = getattr(req, "stop", None)
        if hasattr(req, "stream"):
            provider_params["stream"] = getattr(req, "stream", None)
        
        # 存储Provider参数
        stored_params["provider_params"] = provider_params
        
        self.pending_requests[request_key] = stored_params

        logger.debug(f"已存储LLM请求参数（含完整人格信息和sender信息）: {request_key}")

    def _is_truncated(self, text_or_response) -> bool:
        """主入口方法：多层截断检测，支持文本和LLMResponse对象"""

        # 新增：处理LLMResponse对象
        if hasattr(text_or_response, "completion_text"):
            # 这是一个LLMResponse对象
            resp = text_or_response
            text = resp.completion_text or ""

            # 检查截断标记
            if "[TRUNCATED_BY_LENGTH]" in text:
                logger.debug("LLMResponse对象中检测到截断标记")
                return True

            # 检查raw_completion
            if (
                hasattr(resp, "raw_completion")
                and resp.raw_completion
                and hasattr(resp.raw_completion, "choices")
                and resp.raw_completion.choices
                and getattr(resp.raw_completion.choices[0], "finish_reason", None)
                == "length"
            ):
                logger.debug("LLMResponse对象的raw_completion检测到length截断")
                return True
        else:
            # 这是普通文本
            text = text_or_response

        # 继续现有的检测逻辑...
        if not text or not text.strip():
            return False

        # 如果内容太短，一般不认为是截断（除非明显不完整）
        if len(text.strip()) < self.min_reasonable_length:
            return False

        try:
            # 根据检测模式选择策略
            if self.truncation_detection_mode == "basic":
                return self._detect_character_level_truncation(text)
            elif self.truncation_detection_mode == "enhanced":
                # 多层检测：字符级 + 结构级 + 内容类型
                return (
                    self._detect_character_level_truncation(text)
                    or self._detect_structural_truncation(text)
                    or self._detect_content_type_truncation(text)
                )
            elif self.truncation_detection_mode == "strict":
                # 严格模式：所有检测都要通过
                return self._detect_character_level_truncation(
                    text
                ) and self._detect_structural_truncation(text)
            else:
                # 默认使用基础模式
                return self._detect_character_level_truncation(text)
        except Exception as e:
            logger.warning(f"截断检测发生错误，回退到基础模式: {e}")
            return self._detect_character_level_truncation(text)

    def _detect_character_level_truncation(self, text: str) -> bool:
        """第一层：增强的字符级截断检测"""
        if not text or not text.strip():
            return False

        # 只检测最后一行（防止多段回复）
        last_line = text.strip().splitlines()[-1]

        # 使用增强的正则表达式
        enhanced_pattern = (
            # 原有的模式
            self.truncation_valid_tail_pattern
            +
            # 新增的技术符号
            r"|[->=:]+$|[}\])]$|[0-9]+[%°]?$"
            +
            # 新增的代码文件后缀
            r"|\.(py|js|ts|java|cpp|c|h|css|html|json|xml|yaml|yml|md|rst)$"
        )

        if re.search(enhanced_pattern, last_line, re.IGNORECASE):
            return False
        return True

    def _detect_structural_truncation(self, text: str) -> bool:
        """第二层：结构完整性检测"""
        if not self.check_structural_integrity:
            return False

        try:
            # 检查括号匹配
            if not self._check_bracket_balance(text):
                logger.debug("检测到括号不匹配，可能被截断")
                return True

            # 检查引号匹配
            if self.quote_matching_detection and not self._check_quote_balance(text):
                logger.debug("检测到引号不匹配，可能被截断")
                return True

            # 检查代码块完整性
            if self.code_block_detection and not self._check_markdown_completeness(
                text
            ):
                logger.debug("检测到代码块不完整，可能被截断")
                return True

            return False
        except Exception as e:
            logger.debug(f"结构检测出错，跳过: {e}")
            return False

    def _detect_content_type_truncation(self, text: str) -> bool:
        """第三层：内容类型自适应检测"""
        if not self.check_content_type_specific:
            return False

        try:
            content_type = self._get_content_type(text)

            if content_type == "code":
                return self._is_code_truncated(text)
            elif content_type == "list":
                return self._is_list_truncated(text)
            elif content_type == "table":
                return self._is_table_truncated(text)
            elif content_type == "json":
                return self._is_json_truncated(text)
            else:
                # 自然语言检测
                return self._is_natural_language_truncated(text)
        except Exception as e:
            logger.debug(f"内容类型检测出错，跳过: {e}")
            return False

    def _check_bracket_balance(self, text: str) -> bool:
        """检查括号是否平衡"""
        brackets = {"(": ")", "[": "]", "{": "}", "<": ">"}
        stack = []

        for char in text:
            if char in brackets:
                stack.append(char)
            elif char in brackets.values():
                if not stack:
                    return False
                last_open = stack.pop()
                if brackets[last_open] != char:
                    return False

        # 如果还有未匹配的开括号，可能被截断
        return len(stack) == 0

    def _check_quote_balance(self, text: str) -> bool:
        """检查引号是否平衡"""
        # 检查双引号
        double_quotes = text.count('"') - text.count('\\"')  # 排除转义引号
        if double_quotes % 2 != 0:
            return False

        # 检查单引号（更复杂，因为可能是撇号）
        single_quotes = text.count("'") - text.count("\\'")
        # 对于单引号，我们更宽松一些，只在明显不匹配时判断为截断
        if single_quotes > 2 and single_quotes % 2 != 0:
            return False

        return True

    def _check_markdown_completeness(self, text: str) -> bool:
        """检查Markdown结构完整性"""
        # 检查代码块
        code_blocks = text.count("```")
        if code_blocks % 2 != 0:
            return False

        # 检查行内代码
        inline_code = text.count("`") - text.count("\\`")
        if inline_code % 2 != 0:
            return False

        return True

    def _get_content_type(self, text: str) -> str:
        """识别内容类型"""
        text_lower = text.lower().strip()

        # 代码检测
        if (
            text.count("```") >= 2
            or re.search(
                r"^\s*(def|function|class|import|from|#include)", text, re.MULTILINE
            )
            or text.count("{") > 2
            and text.count("}") > 2
        ):
            return "code"

        # JSON检测
        if (text_lower.startswith("{") and text_lower.endswith("}")) or (
            text_lower.startswith("[") and text_lower.endswith("]")
        ):
            return "json"

        # 列表检测
        if re.search(r"^\s*[-*+]\s+", text, re.MULTILINE) or re.search(
            r"^\s*\d+\.\s+", text, re.MULTILINE
        ):
            return "list"

        # 表格检测
        if "|" in text and text.count("|") > 3:
            return "table"

        return "natural_language"

    def _is_code_truncated(self, text: str) -> bool:
        """检测代码是否被截断"""
        # 检查是否在字符串中间截断
        if text.endswith('"') is False and '"' in text and text.count('"') % 2 == 1:
            return True

        # 检查是否在注释中间截断
        lines = text.splitlines()
        if (
            lines
            and lines[-1].strip().startswith("#")
            and not lines[-1].strip().endswith(".")
        ):
            return True

        return False

    def _is_list_truncated(self, text: str) -> bool:
        """检测列表是否被截断"""
        lines = text.strip().splitlines()
        if not lines:
            return False

        last_line = lines[-1].strip()
        # 如果最后一行是未完成的列表项
        if re.match(r"^\s*[-*+]\s*$", last_line) or re.match(
            r"^\s*\d+\.\s*$", last_line
        ):
            return True

        return False

    def _is_table_truncated(self, text: str) -> bool:
        """检测表格是否被截断"""
        lines = text.strip().splitlines()
        if not lines:
            return False

        # 检查最后一行是否是不完整的表格行
        last_line = lines[-1]
        if "|" in last_line and not last_line.strip().endswith("|"):
            return True

        return False

    def _is_json_truncated(self, text: str) -> bool:
        """检测JSON是否被截断"""
        try:
            json.loads(text)
            return False  # 能解析说明完整
        except json.JSONDecodeError:
            return True  # 解析失败可能是截断

    def _is_natural_language_truncated(self, text: str) -> bool:
        """检测自然语言是否被截断"""
        # 如果以连接词结尾，可能被截断
        conjunctions = [
            "and",
            "or",
            "but",
            "however",
            "therefore",
            "而且",
            "但是",
            "然而",
            "因此",
            "所以",
        ]
        last_words = text.strip().split()[-3:]  # 检查最后几个词

        for word in last_words:
            if word.lower() in conjunctions:
                return True

        return False

    def _extract_status_code(self, text: str) -> Optional[int]:
        """从错误文本中提取 4xx/5xx 状态码"""
        if not text:
            return None
        try:
            match = re.search(r"\b([45]\d{2})\b", text)
            if match:
                return int(match.group(1))
        except Exception:
            pass
        return None

    def _should_retry_response(self, result) -> bool:
        """判断是否需要重试（重构后的检测逻辑）"""
        if not result:
            logger.debug("结果为空，需要重试")
            return True

        # 检查是否有实际内容
        has_content = False
        if hasattr(result, "chain"):
            for comp in result.chain:
                # 任何非Plain类型的消息段都算作有内容
                if not isinstance(comp, Comp.Plain):
                    has_content = True
                    break
                # Plain类型但text非空
                if hasattr(comp, "text") and str(comp.text).strip():
                    has_content = True
                    break

        if not has_content:
            logger.debug("检测到空回复，需要重试")
            return True

        # 检查错误关键词和状态码
        message_str = (
            result.get_plain_text() if hasattr(result, "get_plain_text") else ""
        )
        if message_str:
            # 检查是否包含截断标记
            if "[TRUNCATED_BY_LENGTH]" in message_str:
                logger.debug("检测到截断标记，需要重试")
                return True

            # 状态码检测
            code = self._extract_status_code(message_str)
            if code is not None:
                if code in self.non_retryable_status_codes:
                    logger.debug(f"检测到状态码 {code}，配置为不可重试，跳过重试")
                    return False
                if code in self.retryable_status_codes:
                    logger.debug(f"检测到状态码 {code}，配置允许重试")
                    return True

            # 关键词检测
            lower_message_str = message_str.lower()
            for keyword in self.error_keywords:
                if keyword in lower_message_str:
                    logger.debug(f"检测到错误关键词 '{keyword}'，需要重试")
                    return True

            # 截断检测 - 已移至 retry_on_llm_response 中使用更精确的 finish_reason 判断
            # 这里不再进行基于文本的截断检测，避免误报

        return False

    async def _perform_retry_with_stored_params(
        self, request_key: str
    ) -> Optional[Any]:
        """使用存储的参数执行重试（重构版本：简化sender处理，增加参数验证）"""
        if request_key not in self.pending_requests:
            logger.warning(f"未找到存储的请求参数: {request_key}")
            return None

        stored_params = self.pending_requests[request_key]
        
        # === 参数验证阶段 ===
        # 验证核心参数是否存在
        required_params = ["prompt", "unified_msg_origin"]
        missing_params = [p for p in required_params if p not in stored_params]
        
        if missing_params:
            logger.error(
                f"存储的参数缺少必要字段: {', '.join(missing_params)}。"
                f"这可能是插件版本不兼容导致的。跳过重试。"
            )
            return None
        
        # 验证prompt不为空
        if not stored_params["prompt"] or not str(stored_params["prompt"]).strip():
            logger.error("存储的prompt参数为空，无法进行重试")
            return None
        
        # 获取Provider
        provider = self.context.get_using_provider()
        if not provider:
            logger.warning("LLM提供商未启用，无法重试。")
            return None

        try:
            # === 构建重试参数 ===
            kwargs = {
                "prompt": stored_params["prompt"],
                "image_urls": stored_params.get("image_urls", []),
                "func_tool": stored_params.get("func_tool", None),
            }
            
            # === 鲁棒的 system_prompt 处理逻辑 ===
            # 核心思路：优先实时获取，失败则回退到存储值
            system_prompt = None
            conversation = stored_params.get("conversation")

            if conversation and hasattr(conversation, "persona_id") and conversation.persona_id:
                try:
                    # 使用 getattr 解决 Pylance 因类型提示不完整而误报的问题
                    persona_mgr = getattr(self.context, "persona_manager", None)
                    if persona_mgr:
                        persona = await persona_mgr.get_persona(conversation.persona_id)
                        if persona and persona.system_prompt:
                            system_prompt = persona.system_prompt
                            logger.debug(f"重试时成功从 Persona '{persona.persona_id}' 实时加载 system_prompt")
                    else:
                        logger.warning("重试时无法获取 persona_manager，将回退到存储值")
                except Exception as e:
                    logger.warning(f"重试时实时加载 Persona 失败，将回退到存储值: {e}")

            # 如果实时获取失败，则使用存储的值作为兜底
            if not system_prompt:
                system_prompt = stored_params.get("system_prompt")
                if system_prompt:
                    logger.debug("重试时使用初次请求存储的 system_prompt 作为兜底")

            # 只有在最终获取到 system_prompt 时才添加到参数中
            if system_prompt:
                kwargs["system_prompt"] = system_prompt
            
            # === 简化的sender处理逻辑 ===
            # 策略：优先使用conversation，其次直接传递sender参数
            conversation = stored_params.get("conversation")
            sender_info = stored_params.get("sender", {})
            
            if conversation:
                kwargs["conversation"] = conversation
                
                # 标准方法：设置sender到conversation
                if sender_info:
                    self._attach_sender_to_conversation(conversation, sender_info)
            else:
                # 无conversation时，使用contexts
                kwargs["contexts"] = stored_params.get("contexts", [])
                
                # 直接传递sender作为独立参数（某些Provider可能支持）
                if sender_info:
                    kwargs["sender"] = sender_info
            
            # === 恢复Provider特定参数 ===
            if "provider_params" in stored_params:
                provider_params = stored_params["provider_params"]
                # 只添加非None的参数
                for param_name, param_value in provider_params.items():
                    if param_value is not None:
                        kwargs[param_name] = param_value
                        logger.debug(f"恢复Provider参数: {param_name}={param_value}")

            logger.debug(
                f"正在执行重试，prompt前50字符: '{stored_params['prompt'][:50]}...'"
            )

            llm_response = await provider.text_chat(**kwargs)
            return llm_response

        except Exception as e:
            logger.error(f"重试调用LLM时发生错误: {e}", exc_info=True)
            return None
    
    def _attach_sender_to_conversation(self, conversation, sender_info: dict) -> None:
        """
        将sender信息附加到conversation对象的辅助方法
        采用最可靠的单一方法，避免复杂的条件判断
        """
        if not conversation or not sender_info:
            return
        
        try:
            # 标准方法：设置到metadata（大多数情况下都支持）
            if not hasattr(conversation, "metadata"):
                conversation.metadata = {}
            elif conversation.metadata is None:
                conversation.metadata = {}
            
            conversation.metadata["sender"] = sender_info
            logger.debug("已将sender信息设置到conversation.metadata")
            
        except Exception as e:
            logger.debug(f"设置sender信息时出现异常（已忽略）: {e}")

    async def _execute_retry_sequence(
        self, event: AstrMessageEvent, request_key: str
    ) -> bool:
        """执行重试序列（支持顺序和并发两种模式）"""
        delay = max(0, int(self.retry_delay))

        # 如果未启用并发重试，使用原有的顺序重试逻辑
        if not self.enable_concurrent_retry:
            return await self._sequential_retry_sequence(
                event, request_key, self.max_attempts, delay
            )

        # 并发重试模式：根据阈值决定是否跳过顺序重试
        if self.concurrent_retry_threshold == 0:
            # 阈值为0：直接启用并发重试，使用全部重试次数
            logger.info("配置为直接并发重试模式，跳过顺序重试阶段")
            return await self._concurrent_retry_sequence(
                event, request_key, self.max_attempts
            )

        # 混合重试模式：先顺序重试到阈值，然后并发重试
        sequential_attempts = min(self.concurrent_retry_threshold, self.max_attempts)
        logger.info(f"混合重试模式：先 {sequential_attempts} 次顺序，后并发")

        # 第一阶段：顺序重试
        if sequential_attempts > 0:
            logger.debug(f"开始顺序重试阶段（{sequential_attempts} 次）")
            sequential_success = await self._sequential_retry_sequence(
                event, request_key, sequential_attempts, delay
            )
            if sequential_success:
                return True

        # 第二阶段：并发重试（如果还有剩余尝试次数）
        remaining_attempts = self.max_attempts - sequential_attempts
        if remaining_attempts > 0:
            logger.debug(
                f"顺序重试失败，切换到并发重试阶段（剩余 {remaining_attempts} 次）"
            )
            return await self._concurrent_retry_sequence(
                event, request_key, remaining_attempts
            )

        return False

    async def _sequential_retry_sequence(
        self,
        event: AstrMessageEvent,
        request_key: str,
        max_attempts: int,
        initial_delay: int,
    ) -> bool:
        """顺序重试序列（从原_execute_retry_sequence方法拆分出来）"""
        delay = initial_delay

        for attempt in range(1, max_attempts + 1):
            logger.info(f"第 {attempt}/{max_attempts} 次重试...")

            new_response = await self._perform_retry_with_stored_params(request_key)

            if not new_response or not getattr(new_response, "completion_text", ""):
                logger.warning(f"第 {attempt} 次重试返回空结果")
            else:
                new_text = new_response.completion_text.strip()

                # 检查新回复是否包含错误
                new_text_lower = new_text.lower()
                has_error = any(
                    keyword in new_text_lower for keyword in self.error_keywords
                )

                # 状态码检测
                code = self._extract_status_code(new_text)
                if code is not None:
                    if code in self.non_retryable_status_codes:
                        logger.warning(f"检测到不可重试状态码 {code}，提前结束重试")
                        return False
                    if code in self.retryable_status_codes:
                        has_error = True

                if new_text and not has_error:
                    logger.info(f"第 {attempt} 次重试成功，生成有效回复")
                    # 确保重试结果被正确标记为LLM结果，以便TTS等插件能正确处理
                    from astrbot.api.event import MessageEventResult
                    from astrbot.api.event import ResultContentType

                    result = MessageEventResult()
                    result.message(new_text)
                    result.result_content_type = ResultContentType.LLM_RESULT
                    event.set_result(result)
                    return True
                else:
                    logger.warning(
                        f"第 {attempt} 次重试仍包含错误或为空: {new_text[:100]}..."
                    )

            # 等待后重试
            if attempt < max_attempts and delay > 0:
                await asyncio.sleep(delay)
                if self.retry_delay_mode == "exponential":
                    delay = min(delay * 2, 30)

        return False

    async def _concurrent_retry_sequence(
        self, event: AstrMessageEvent, request_key: str, remaining_attempts: int
    ) -> bool:
        """并发重试序列，遵循AstrBot异步处理规范"""
        if remaining_attempts <= 0:
            return False

        attempts_used = 0
        batch_number = 1

        while attempts_used < remaining_attempts:
            # 计算指数增长的并发数，但有合理上限控制
            if self.enable_exponential_growth:
                base_count = self.concurrent_retry_count
                exponential_count = base_count * (
                    2 ** (batch_number - 1)
                )  # 2, 4, 8, 16...

                current_concurrent_count = min(
                    exponential_count,  # 指数增长的并发数
                    remaining_attempts - attempts_used,  # 不超过剩余次数
                    self.concurrent_retry_count
                    * self.max_concurrent_multiplier,  # 上限：基础并发数的倍数
                    self.absolute_concurrent_limit,  # 绝对上限
                )
                growth_info = f"(指数增长: {base_count}×{2 ** (batch_number - 1)}={exponential_count}, 实际={current_concurrent_count})"
            else:
                current_concurrent_count = min(
                    self.concurrent_retry_count,  # 固定并发数量
                    remaining_attempts - attempts_used,  # 不超过剩余次数
                )
                growth_info = "(固定并发)"

            logger.info(
                f"启动第 {batch_number} 批次并发重试，并发数: {current_concurrent_count} {growth_info}"
            )

            # 执行并发批次 - 遵循官方异步规范
            batch_success = await self._single_concurrent_batch(
                event, request_key, current_concurrent_count
            )
            if batch_success:
                return True

            # 更新计数器
            attempts_used += current_concurrent_count
            batch_number += 1

            logger.debug(
                f"第 {batch_number - 1} 批次失败，已用 {attempts_used}/{remaining_attempts} 次重试"
            )

            # 批次间延迟 - 遵循官方性能规范，避免过于频繁请求
            if attempts_used < remaining_attempts:
                await asyncio.sleep(1)

        logger.warning(f"所有 {batch_number - 1} 个并发批次均失败")
        return False

    async def _single_concurrent_batch(
        self, event: AstrMessageEvent, request_key: str, concurrent_count: int
    ) -> bool:
        """执行单个并发批次"""
        # 用于存储第一个有效结果
        first_valid_result = None
        result_lock = asyncio.Lock()

        async def single_concurrent_attempt(attempt_id: int):
            """单个并发重试任务"""
            nonlocal first_valid_result

            try:
                logger.debug(f"并发重试任务 #{attempt_id} 开始")
                new_response = await self._perform_retry_with_stored_params(request_key)

                if not new_response or not getattr(new_response, "completion_text", ""):
                    logger.debug(f"并发重试任务 #{attempt_id} 返回空结果")
                    return None

                new_text = new_response.completion_text.strip()

                # 检查新回复是否包含错误
                new_text_lower = new_text.lower()
                has_error = any(
                    keyword in new_text_lower for keyword in self.error_keywords
                )

                # 状态码检测
                code = self._extract_status_code(new_text)
                if code is not None:
                    if code in self.non_retryable_status_codes:
                        logger.debug(
                            f"并发重试任务 #{attempt_id} 检测到不可重试状态码 {code}"
                        )
                        return None
                    if code in self.retryable_status_codes:
                        has_error = True

                if new_text and not has_error:
                    # 使用锁确保线程安全
                    async with result_lock:
                        if first_valid_result is None:
                            first_valid_result = new_text
                            logger.info(f"并发重试任务 #{attempt_id} 获得首个有效结果")
                            return new_text
                        else:
                            logger.debug(
                                f"并发重试任务 #{attempt_id} 获得结果但已有首个结果，丢弃"
                            )
                            return None
                else:
                    logger.debug(f"并发重试任务 #{attempt_id} 结果包含错误或为空")
                    return None

            except Exception as e:
                logger.error(f"并发重试任务 #{attempt_id} 发生异常: {e}")
                return None

        # 创建并发任务
        tasks = [
            asyncio.create_task(single_concurrent_attempt(i))
            for i in range(1, concurrent_count + 1)
        ]

        try:
            # 使用循环等待，确保"等待首个成功或全部失败"的正确逻辑
            remaining_tasks = set(tasks)
            start_time = asyncio.get_event_loop().time()

            while remaining_tasks and not first_valid_result:
                # 检查超时
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed >= self.concurrent_retry_timeout:
                    logger.warning(f"并发重试超时（{self.concurrent_retry_timeout}s）")
                    break

                # 等待至少一个任务完成，使用剩余时间作为超时
                remaining_timeout = max(0.1, self.concurrent_retry_timeout - elapsed)
                done, still_pending = await asyncio.wait(
                    remaining_tasks,
                    timeout=remaining_timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                # 更新剩余任务集合
                remaining_tasks = still_pending

                # 检查完成的任务（但不处理结果，结果已在single_concurrent_attempt中处理）
                for task in done:
                    try:
                        await task  # 确保任务完全完成
                    except Exception as e:
                        logger.debug(f"任务完成时出现异常（已忽略）: {e}")

                # 如果已经有有效结果，立即跳出循环
                if first_valid_result:
                    break

            # 取消所有剩余任务
            for task in remaining_tasks:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

            # 检查最终结果 - 遵循官方结果处理规范
            if first_valid_result:
                # 确保并发重试结果也被正确标记为LLM结果
                from astrbot.api.event import MessageEventResult
                from astrbot.api.event import ResultContentType

                result = MessageEventResult()
                result.message(first_valid_result)
                result.result_content_type = ResultContentType.LLM_RESULT
                event.set_result(result)

                # 清理剩余任务 - 遵循官方资源管理规范
                cancelled_count = len([t for t in remaining_tasks if not t.done()])
                if cancelled_count > 0:
                    logger.debug(f"并发重试成功，已取消 {cancelled_count} 个剩余任务")
                else:
                    logger.info("并发重试成功完成")

                return True
            else:
                logger.debug(f"并发批次未获得有效结果，{concurrent_count} 个任务均失败")
                return False

        except Exception as e:
            logger.error(f"并发批次执行异常: {e}")
            # 确保异常情况下的资源清理
            await self._cleanup_concurrent_tasks(tasks)

            # 检查是否有结果可用
            if first_valid_result:
                from astrbot.api.event import MessageEventResult
                from astrbot.api.event import ResultContentType

                result = MessageEventResult()
                result.message(first_valid_result)
                result.result_content_type = ResultContentType.LLM_RESULT
                event.set_result(result)
                logger.info("异常期间获得有效结果，仍然返回成功")
                return True

            return False

    def _handle_retry_failure(self, event: AstrMessageEvent) -> None:
        """处理重试失败的情况，遵循AstrBot事件处理规范"""
        logger.error(f"所有 {self.max_attempts} 次重试均失败")

        # 发送兜底回复
        if self.fallback_reply and self.fallback_reply.strip():
            # 确保兜底回复也被标记为LLM结果
            from astrbot.api.event import MessageEventResult
            from astrbot.api.event import ResultContentType

            result = MessageEventResult()
            result.message(self.fallback_reply.strip())
            result.result_content_type = ResultContentType.LLM_RESULT
            event.set_result(result)
            logger.info("已发送兜底回复消息（标记为LLM结果）")
        else:
            event.clear_result()
            event.stop_event()
            logger.debug("未配置兜底回复，事件已终止")

    @filter.on_llm_response(priority=10)
    async def retry_on_llm_response(self, event: AstrMessageEvent, resp):
        """在LLM响应阶段进行重试检测和处理"""
        # 检查类型 - 使用鸭子类型检查而不是isinstance以避免导入问题
        if not hasattr(resp, "completion_text"):
            logger.warning(
                "retry_on_llm_response: Expected LLMResponse-like object but got different type"
            )
            return
        # 如果禁用重试则直接返回
        if self.max_attempts <= 0:
            return

        # 检查是否有存储的请求参数
        request_key = self._get_request_key(event)
        if request_key not in self.pending_requests:
            return

        # 使用现有的响应失败检测逻辑
        # 这里我们需要将LLMResponse转换为类似result的格式来复用现有逻辑
        should_retry = False

        # 检测底层provider的截断标记
        if resp.completion_text and "[TRUNCATED_BY_LENGTH]" in resp.completion_text:
            should_retry = True
            logger.info("检测到provider层面的截断标记，需要重试")
            # 清理截断标记
            resp.completion_text = resp.completion_text.replace(
                "[TRUNCATED_BY_LENGTH]", ""
            ).strip()

        # 核心修改：更智能的截断检测 - 必须同时满足 finish_reason 和文本特征
        elif (
            self.enable_truncation_retry
            and resp  # 确保 resp 对象存在
            and hasattr(resp, "finish_reason")  # 首先检查是否有 finish_reason 属性
            and resp.finish_reason == "length"  # 核心条件：finish_reason 必须是 'length'
        ):
            # finish_reason 是 'length'，现在检查文本特征作为辅助判断
            if resp.completion_text and resp.completion_text.strip():
                # 使用现有的文本截断检测作为辅助判断
                if self._is_truncated(resp.completion_text):
                    should_retry = True
                    logger.info(
                        f"检测到LLM响应被截断（finish_reason='length' 且文本特征符合）。"
                        f"内容结尾: {resp.completion_text[-20:]}"
                    )
                else:
                    logger.debug(
                        "虽然 finish_reason='length'，但文本看起来是完整的，不触发重试"
                    )
            else:
                # finish_reason 是 'length' 但没有文本内容，这种情况应该重试
                should_retry = True
                logger.info("检测到 finish_reason='length' 且响应为空，需要重试")
        
        # 检查 raw_completion 中的 finish_reason（兼容不同的响应结构）
        elif (
            self.enable_truncation_retry
            and hasattr(resp, "raw_completion")
            and resp.raw_completion
            and hasattr(resp.raw_completion, "choices")
            and resp.raw_completion.choices
            and getattr(resp.raw_completion.choices[0], "finish_reason", None) == "length"
        ):
            # 同样需要检查文本特征
            if resp.completion_text and resp.completion_text.strip():
                if self._is_truncated(resp.completion_text):
                    should_retry = True
                    logger.info(
                        f"检测到raw_completion中的length截断且文本特征符合。"
                        f"内容结尾: {resp.completion_text[-20:]}"
                    )
                else:
                    logger.debug(
                        "虽然 raw_completion.finish_reason='length'，但文本看起来是完整的，不触发重试"
                    )
            else:
                should_retry = True
                logger.info("检测到 raw_completion.finish_reason='length' 且响应为空，需要重试")

        elif not resp.completion_text or not resp.completion_text.strip():
            # 空回复需要重试
            should_retry = True
            logger.debug("检测到空的LLM响应，需要重试")
        else:
            # 如果有文本内容，创建一个临时result对象来检测其他错误情况（不包括截断）
            from astrbot.api.event import MessageEventResult

            temp_result = MessageEventResult()
            temp_result.chain = [Comp.Plain(text=resp.completion_text)]

            # 使用现有的检测逻辑（但排除截断检测，因为已经在上面处理了）
            should_retry = self._should_retry_response(temp_result)

        if not should_retry:
            return

        logger.info("在LLM响应阶段检测到需要重试的情况")

        # 执行重试序列
        retry_success = await self._execute_retry_sequence(event, request_key)

        if retry_success:
            # 重试成功，更新LLM响应
            result = event.get_result()
            if result and result.chain:
                # 更新completion_text
                text_parts = [
                    comp.text
                    for comp in result.chain
                    if isinstance(comp, Comp.Plain) and hasattr(comp, "text")
                ]
                if text_parts:
                    resp.completion_text = "".join(text_parts)
                    logger.info("LLM响应已通过重试更新")
        else:
            # 重试失败，发送兜底回复
            if self.fallback_reply and self.fallback_reply.strip():
                resp.completion_text = self.fallback_reply.strip()
                logger.info("重试失败，使用兜底回复")
            else:
                # 如果没有兜底回复，保持原样但记录日志
                logger.warning("重试失败且未配置兜底回复")

        # 清理存储的请求参数
        if request_key in self.pending_requests:
            del self.pending_requests[request_key]
            logger.debug(f"LLM响应阶段已清理请求参数: {request_key}")

    @filter.on_decorating_result(priority=-100)
    async def check_and_retry(self, event: AstrMessageEvent, *args, **kwargs):
        """检查结果并进行重试（作为LLM响应钩子的备用处理）"""
        # 如果禁用重试则直接返回
        if self.max_attempts <= 0:
            return

        # 检查是否还有存储的请求参数
        request_key = self._get_request_key(event)
        if request_key not in self.pending_requests:
            # 已经被LLM响应钩子处理过，直接返回
            return

        # 检查原始LLM响应，如果是工具调用则不干预
        llm_response = getattr(event, "llm_response", None)
        if llm_response and hasattr(llm_response, "choices") and llm_response.choices:
            finish_reason = getattr(llm_response.choices[0], "finish_reason", None)
            if finish_reason == "tool_calls":
                logger.debug("检测到正常的工具调用，不进行干预")
                # 清理请求参数
                if request_key in self.pending_requests:
                    del self.pending_requests[request_key]
                return

        result = event.get_result()

        # 检查是否需要重试
        if not self._should_retry_response(result):
            # 清理请求参数
            if request_key in self.pending_requests:
                del self.pending_requests[request_key]
            return

        # 只有在用户发送了文本内容时才进行重试
        if not event.message_str or not event.message_str.strip():
            logger.debug("用户消息为空，跳过重试")
            # 清理请求参数
            if request_key in self.pending_requests:
                del self.pending_requests[request_key]
            return

        logger.info("在结果装饰阶段检测到需要重试的情况（备用处理）")

        # 执行重试序列
        retry_success = await self._execute_retry_sequence(event, request_key)

        # 如果重试失败，处理失败情况
        if not retry_success:
            self._handle_retry_failure(event)

        # 清理存储的请求参数
        if request_key in self.pending_requests:
            del self.pending_requests[request_key]
            logger.debug(f"结果装饰阶段已清理请求参数: {request_key}")

    async def _cleanup_concurrent_tasks(self, tasks):
        """安全清理并发任务，遵循AstrBot资源管理规范"""
        if not tasks:
            return

        cleanup_count = 0
        for task in tasks:
            if not task.done():
                task.cancel()
                cleanup_count += 1
                try:
                    await task
                except asyncio.CancelledError:
                    # 官方推荐：正常的取消操作，不记录为错误
                    pass
                except Exception as e:
                    # 使用官方logger记录清理异常
                    logger.debug(f"清理并发任务时出现异常: {e}")

        if cleanup_count > 0:
            logger.debug(f"已清理 {cleanup_count} 个未完成的并发任务")

    async def terminate(self):
        """插件卸载时清理资源，遵循官方生命周期规范"""
        # 清理存储的请求参数
        self.pending_requests.clear()
        logger.info("已卸载 [IntelligentRetry] 插件并清理所有资源")


# --- END OF FILE main.py ---




