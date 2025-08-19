# --- START OF FILE main.py ---

import asyncio
import json
import re
from typing import Optional, Set, List, Dict, Any, Tuple

import astrbot.api.message_components as Comp
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

@register(
    "intelligent_retry",
    "木有知 & 长安某",
    "当LLM回复为空或包含特定错误关键词时，自动进行多次重试，保持完整上下文和人设",
    "2.7.2"
)
class IntelligentRetry(Star):
    """
    一个AstrBot插件，在检测到LLM回复为空或返回包含特定关键词的错误文本时，
    自动进行多次重试，并完整保持原有的上下文和人设。
    
    V2.7.2: 严重Bug修复版本 - 解决误判正常空消息问题：
    - 修复插件误将AstrBot正常运行中的空消息当作LLM错误进行重试
    - 增加LLM响应来源验证，只对真正的LLM调用结果进行重试判断
    - 检查finish_reason确保是文本完成类型的响应
    - 验证event的call_llm标志确认是LLM调用
    
    V2.7.1: 关键Bug修复版本 - 解决重试逻辑不一致问题：
    - 修复 _should_retry 和 _is_response_valid 状态码判断逻辑矛盾
    - 增强空回复检查逻辑，减少误判
    - 简化方法调用链，提高可靠性
    """
    
    # 预编译正则表达式，避免重复编译
    HTTP_STATUS_PATTERN = re.compile(r"\b([45]\d{2})\b")
    
    # 常量定义
    MAX_RETRY_DELAY = 30
    DEFAULT_MAX_ATTEMPTS = 3
    DEFAULT_RETRY_DELAY = 2
    DEFAULT_PREVIEW_LAST_N = 3
    DEFAULT_PREVIEW_MAX_CHARS = 120

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        
        # 验证和设置基础配置
        self.max_attempts = self._validate_config_int(
            config.get('max_attempts', self.DEFAULT_MAX_ATTEMPTS), 
            'max_attempts', 0, 10, self.DEFAULT_MAX_ATTEMPTS
        )
        self.retry_delay = self._validate_config_float(
            config.get('retry_delay', self.DEFAULT_RETRY_DELAY),
            'retry_delay', 0.1, 60.0, self.DEFAULT_RETRY_DELAY
        )
        
        # 错误关键词处理
        default_keywords = "api 返回的内容为空\nAPI 返回的 completion 由于内容安全过滤被拒绝(非 AstrBot)\n调用失败"
        keywords_str = config.get('error_keywords', default_keywords)
        self.error_keywords = self._parse_keywords(keywords_str)

        # 人设控制配置
        self.always_use_system_prompt = bool(config.get('always_use_system_prompt', True))
        self.fallback_system_prompt_text = str(config.get('fallback_system_prompt', '')).strip()

        # 状态码配置
        self.retryable_status_codes, self.non_retryable_status_codes = self._parse_status_codes(config)

        # 调试配置
        self.log_context_preview = bool(config.get('log_context_preview', False))
        self.context_preview_last_n = self._validate_config_int(
            config.get('context_preview_last_n', self.DEFAULT_PREVIEW_LAST_N),
            'context_preview_last_n', 0, 20, self.DEFAULT_PREVIEW_LAST_N
        )
        self.context_preview_max_chars = self._validate_config_int(
            config.get('context_preview_max_chars', self.DEFAULT_PREVIEW_MAX_CHARS),
            'context_preview_max_chars', 20, 500, self.DEFAULT_PREVIEW_MAX_CHARS
        )

        # 兜底回复
        self.fallback_reply = str(config.get('fallback_reply', 
            "抱歉，刚才遇到服务波动，我已自动为你重试多次仍未成功。请稍后再试或换个说法。"))

        logger.info(
            f"已加载 [IntelligentRetry] 插件 v2.7.2 (关键修复版), "
            f"将在LLM回复无效时自动重试 (最多 {self.max_attempts} 次)，保持完整上下文和人设。"
        )

    def _validate_config_int(self, value: Any, name: str, min_val: int, max_val: int, default: int) -> int:
        """验证整数配置项"""
        try:
            int_val = int(value)
            if min_val <= int_val <= max_val:
                return int_val
            logger.warning(f"配置项 {name}={int_val} 超出范围 [{min_val}, {max_val}]，使用默认值 {default}")
            return default
        except (ValueError, TypeError) as e:
            logger.warning(f"配置项 {name}={value} 解析失败: {e}，使用默认值 {default}")
            return default

    def _validate_config_float(self, value: Any, name: str, min_val: float, max_val: float, default: float) -> float:
        """验证浮点数配置项"""
        try:
            float_val = float(value)
            if min_val <= float_val <= max_val:
                return float_val
            logger.warning(f"配置项 {name}={float_val} 超出范围 [{min_val}, {max_val}]，使用默认值 {default}")
            return default
        except (ValueError, TypeError) as e:
            logger.warning(f"配置项 {name}={value} 解析失败: {e}，使用默认值 {default}")
            return default

    def _parse_keywords(self, keywords_str: str) -> List[str]:
        """解析错误关键词"""
        if not keywords_str:
            return []
        
        keywords = []
        for line in keywords_str.split('\n'):
            keyword = line.strip().lower()
            if keyword and keyword not in keywords:  # 去重
                keywords.append(keyword)
        return keywords

    def _parse_status_codes(self, config: AstrBotConfig) -> Tuple[Set[int], Set[int]]:
        """解析状态码配置"""
        retryable_codes_default = "400\n429\n502\n503\n504"
        non_retryable_codes_default = ""
        
        retryable_codes_str = config.get('retryable_status_codes', retryable_codes_default)
        non_retryable_codes_str = config.get('non_retryable_status_codes', non_retryable_codes_default)

        def parse_codes(s: str) -> Set[int]:
            codes = set()
            for line in s.split('\n'):
                line = line.strip()
                if line.isdigit():
                    try:
                        code = int(line)
                        if 400 <= code <= 599:  # 只接受有效的HTTP错误状态码
                            codes.add(code)
                        else:
                            logger.warning(f"无效的HTTP状态码: {code}，已忽略")
                    except ValueError:
                        logger.warning(f"无法解析状态码: {line}，已忽略")
            return codes

        return parse_codes(retryable_codes_str), parse_codes(non_retryable_codes_str)

    async def _get_complete_context(self, unified_msg_origin: str) -> List[Dict[str, Any]]:
        """获取完整的对话上下文，包括当前消息"""
        if not unified_msg_origin:
            return []
            
        try:
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(unified_msg_origin)
            if not curr_cid:
                return []
            
            conv = await self.context.conversation_manager.get_conversation(unified_msg_origin, curr_cid)
            if not conv or not conv.history:
                return []
            
            # 直接解析JSON，无需线程池 - 修复性能问题
            context_history = json.loads(conv.history)
            return context_history if isinstance(context_history, list) else []
            
        except (json.JSONDecodeError, AttributeError, TypeError) as e:
            logger.error(f"对话上下文解析失败: {e}")
            return []
        except Exception as e:
            logger.error(f"获取对话上下文时发生未知错误: {e}")
            return []

    async def _get_provider_config(self) -> Tuple[Optional[Any], Optional[str], Optional[Any]]:
        """获取 LLM 提供商的完整配置，包括人设"""
        provider = self.context.get_using_provider()
        if not provider:
            return None, None, None
        
        # 获取系统提示词（人设）- 优化属性访问
        system_prompt = None
        try:
            if hasattr(provider, "system_prompt"):
                system_prompt = provider.system_prompt
            elif hasattr(provider, "config") and provider.config:
                system_prompt = provider.config.get("system_prompt")
        except Exception as e:
            logger.warning(f"获取系统提示词时出错: {e}")
        
        # 获取工具配置
        func_tool = None
        try:
            if hasattr(provider, "func_tool"):
                func_tool = provider.func_tool
        except Exception as e:
            logger.warning(f"获取函数工具时出错: {e}")
        
        return provider, system_prompt, func_tool

    def _extract_context_system_info(self, context_history: List[Dict[str, Any]]) -> Tuple[bool, str]:
        """提取上下文中的system消息信息"""
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
            pass  # 忽略解析错误，不影响主流程
            
        return has_system, sys_preview

    def _build_context_preview(self, context_history: List[Dict[str, Any]]) -> str:
        """构建上下文预览字符串 - 优化字符串操作"""
        if not context_history or self.context_preview_last_n <= 0:
            return ""
            
        try:
            tail = context_history[-self.context_preview_last_n:]
            preview_parts = []
            
            for idx, msg in enumerate(tail, 1):
                if isinstance(msg, dict):
                    role = str(msg.get('role', ''))
                    content = msg.get('content', '')
                else:
                    role = ''
                    content = str(msg)
                
                # 优化字符串处理
                try:
                    text = str(content).replace('\n', ' ')
                except Exception:
                    text = '<non-text-content>'
                
                if len(text) > self.context_preview_max_chars:
                    text = text[:self.context_preview_max_chars] + '…'
                
                preview_parts.append(f"#{idx} [{role}] {text}")
            
            return "\n".join(preview_parts)
        except Exception:
            return "<预览生成失败>"

    def _filter_system_messages(self, context_history: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
        """过滤掉上下文中的system消息，返回过滤后的列表和移除的数量"""
        filtered = []
        removed = 0
        
        for msg in context_history:
            if isinstance(msg, dict) and str(msg.get('role', '')).lower() == 'system':
                removed += 1
            else:
                filtered.append(msg)
                
        return filtered, removed

    async def _perform_retry_with_context(self, event: AstrMessageEvent) -> Optional[Any]:
        """执行重试，完整保持原有上下文和人设 - 优化版"""
        provider, system_prompt, func_tool = await self._get_provider_config()
        
        if not provider:
            logger.warning("LLM提供商未启用，无法重试。")
            return None

        try:
            # 获取完整的对话上下文
            context_history = await self._get_complete_context(event.unified_msg_origin)
            
            # 判断上下文中是否已经包含 system 消息
            has_system_in_contexts, sys_preview = self._extract_context_system_info(context_history)
            
            # 获取图片URL - 增强错误处理
            image_urls = []
            try:
                image_urls = [
                    comp.url
                    for comp in event.message_obj.message
                    if isinstance(comp, Comp.Image) and hasattr(comp, "url") and comp.url
                ]
            except Exception as e:
                logger.warning(f"提取图片URL失败: {e}")

            logger.debug(f"正在使用完整上下文进行重试... Prompt: '{event.message_str}'")
            logger.debug(
                f"上下文长度: {len(context_history)}, 系统提示词存在: {system_prompt is not None}, "
                f"上下文含system: {has_system_in_contexts}"
                f"{'，示例: ' + sys_preview if has_system_in_contexts and sys_preview else ''}"
            )

            # 可选：输出最近 N 条上下文预览（仅 DEBUG 日志）- 优化性能
            if self.log_context_preview and context_history and self.context_preview_last_n > 0:
                try:
                    preview = self._build_context_preview(context_history)
                    if preview:
                        logger.debug(f"上下文预览(最近 {self.context_preview_last_n} 条):\n{preview}")
                except Exception:
                    # 预览失败不影响主流程
                    pass

            # 处理强制人设覆盖逻辑
            if self.always_use_system_prompt:
                # 若 Provider 无人设而插件提供了备用人设，则使用备用人设
                if not system_prompt and self.fallback_system_prompt_text:
                    system_prompt = self.fallback_system_prompt_text
                    logger.debug("Provider 未提供 system_prompt，已启用插件的 fallback_system_prompt 作为人设")

                if system_prompt:
                    # 移除上下文中的所有 system 消息 - 使用优化后的方法
                    context_history, removed = self._filter_system_messages(context_history)
                    if removed > 0:
                        logger.debug(f"已强制覆盖人设：移除 {removed} 条历史 system 消息")
                    # 更新标记
                    has_system_in_contexts = False
                else:
                    logger.warning("配置了 always_use_system_prompt，但 Provider 未提供 system_prompt，已回退为上下文判断模式")
            
            # 构建请求参数
            kwargs = {
                'prompt': event.message_str,
                'contexts': context_history,
                'image_urls': image_urls,
                'func_tool': func_tool,
            }
            
            # 决定是否传入 system_prompt
            if self.always_use_system_prompt and system_prompt:
                kwargs['system_prompt'] = system_prompt
            elif not self.always_use_system_prompt and not has_system_in_contexts and system_prompt:
                kwargs['system_prompt'] = system_prompt

            # 执行LLM调用 - 增强错误处理
            if not provider:  # 双重检查，防止provider在调用过程中被卸载
                logger.warning("Provider在重试过程中不可用")
                return None
                
            llm_response = await provider.text_chat(**kwargs)
            return llm_response
            
        except Exception as e:
            logger.error(f"重试调用LLM时发生错误: {e}")
            return None

    def _extract_status_code(self, text: str) -> Optional[int]:
        """
        从错误文本中提取 4xx/5xx 状态码 - 优化版
        使用预编译的正则表达式，提升性能
        """
        if not text:
            return None
            
        try:
            match = self.HTTP_STATUS_PATTERN.search(text)
            return int(match.group(1)) if match else None
        except (ValueError, AttributeError):
            return None

    def _should_retry(self, result) -> bool:
        """
        判断是否需要重试 - 修复版
        判定顺序（从高到低）：
        1) 结果对象为空 -> 重试
        2) 消息链为空或没有有效内容 -> 重试
        3) 文本中解析到状态码：
           - 命中 non_retryable_status_codes -> 不重试（优先级最高）
           - 命中 retryable_status_codes    -> 重试
        4) 命中错误关键词 -> 重试
        5) 其它情况 -> 不重试
        """
        # 检查结果对象本身
        if not result:
            logger.debug("结果对象为空，需要重试")
            return True
        
        # 检查消息链是否存在
        if not hasattr(result, 'chain') or not result.chain:
            logger.debug("消息链为空，需要重试")
            return True
        
        # 检查是否有实际文本内容
        has_valid_content = False
        plain_text_parts = []
        
        try:
            for component in result.chain:
                if isinstance(component, Comp.Plain):
                    text_content = component.text.strip() if hasattr(component, 'text') else ""
                    if text_content:
                        has_valid_content = True
                        plain_text_parts.append(text_content)
                else:
                    # 非文本组件（如图片、语音等）也算作有效内容
                    has_valid_content = True
        except Exception as e:
            logger.warning(f"检查消息链内容时出错: {e}")
            return True  # 出错时默认重试
        
        if not has_valid_content:
            logger.debug("检测到空回复（无有效内容），需要重试")
            return True
        
        # 获取完整的文本内容进行错误检查
        full_text = " ".join(plain_text_parts).strip()
        
        if not full_text:
            logger.debug("检测到空文本回复，需要重试")
            return True
            
        # 检查状态码
        code = self._extract_status_code(full_text)
        if code is not None:
            if code in self.non_retryable_status_codes:
                logger.debug(f"检测到状态码 {code}，配置为不可重试，跳过重试")
                return False
            if code in self.retryable_status_codes:
                logger.debug(f"检测到状态码 {code}，配置允许重试")
                return True
            
        # 检查错误关键词
        text_lower = full_text.lower()
        for keyword in self.error_keywords:
            if keyword in text_lower:
                logger.debug(f"检测到错误关键词 '{keyword}'，需要重试")
                return True
        
        # 没有发现需要重试的条件
        return False

    @filter.on_decorating_result(priority=-1)
    async def check_and_retry(self, event: AstrMessageEvent):
        """
        检查结果并进行重试，保持完整的上下文和人设 - 修复版
        
        关键修复：只对真正的LLM响应进行重试判断，避免误判正常的空消息
        """
        # 早期退出检查
        if self.max_attempts <= 0:
            return

        # 🚨 关键修复：检查是否存在LLM响应，只有LLM调用才考虑重试
        _llm_response = getattr(event, 'llm_response', None)
        
        # 如果没有LLM响应，说明这不是LLM调用产生的结果，不进行干预
        if not _llm_response:
            logger.debug("未检测到LLM响应，跳过重试检查（可能是插件或指令产生的空消息）")
            return
        
        # 检查是否是工具调用，工具调用不干预
        try:
            if (hasattr(_llm_response, 'choices') and 
                _llm_response.choices and 
                getattr(_llm_response.choices[0], 'finish_reason', None) == 'tool_calls'):
                logger.debug("检测到正常的工具调用，不进行干预")
                return
        except Exception:
            pass  # 忽略检查错误，继续执行

        # 🚨 关键修复：进一步验证这确实是LLM文本生成的结果
        # 检查LLM响应是否表明这是一个文本完成请求
        try:
            if (hasattr(_llm_response, 'choices') and 
                _llm_response.choices):
                finish_reason = getattr(_llm_response.choices[0], 'finish_reason', None)
                # 只对文本完成类型的响应进行重试判断
                if finish_reason not in ['stop', 'length', None]:
                    logger.debug(f"LLM响应finish_reason为 {finish_reason}，不是文本完成，跳过重试")
                    return
        except Exception:
            pass

        # 获取并检查结果 - 现在确认这是LLM产生的结果
        result = event.get_result()
        if not self._should_retry(result):
            return
        
        # 验证用户消息 - 确保这是用户主动发起的对话
        if not event.message_str or not event.message_str.strip():
            logger.debug("用户消息为空，跳过重试（可能是系统消息或非对话消息）")
            return

        # 🚨 关键修复：额外检查 - 确保event确实调用了LLM
        # 通过检查event的call_llm标志来确认
        if hasattr(event, 'call_llm') and not event.call_llm:
            logger.debug("事件未标记为LLM调用，跳过重试检查")
            return

        logger.info("检测到LLM响应需要重试的情况，开始重试流程")

        # 执行重试流程 - 优化指数退避算法
        success = await self._execute_retry_loop(event)
        
        # 处理最终结果
        if not success:
            logger.error(f"所有 {self.max_attempts} 次重试均失败")
            self._handle_retry_failure(event)

    async def _execute_retry_loop(self, event: AstrMessageEvent) -> bool:
        """执行重试循环 - 分离出来提高可读性"""
        delay = max(0.1, float(self.retry_delay))  # 确保最小延时
        
        for attempt in range(1, self.max_attempts + 1):
            logger.info(f"第 {attempt}/{self.max_attempts} 次重试...")

            # 执行重试
            new_response = await self._perform_retry_with_context(event)

            # 检查响应有效性
            if not new_response or not getattr(new_response, 'completion_text', ''):
                logger.warning(f"第 {attempt} 次重试返回空结果")
                if attempt < self.max_attempts:
                    await self._apply_retry_delay(delay)
                    delay = min(delay * 2, self.MAX_RETRY_DELAY)
                continue

            # 验证响应内容
            new_text = new_response.completion_text.strip()
            if self._is_response_valid(new_text):
                logger.info(f"第 {attempt} 次重试成功，生成有效回复")
                event.set_result(event.plain_result(new_text))
                return True
            else:
                logger.warning(f"第 {attempt} 次重试仍包含错误或为空: {new_text[:100]}...")
                if attempt < self.max_attempts:
                    await self._apply_retry_delay(delay)
                    delay = min(delay * 2, self.MAX_RETRY_DELAY)

        return False

    def _is_response_valid(self, text: str) -> bool:
        """
        检查响应是否有效 - 修复版，与_should_retry逻辑保持一致
        返回 True 表示响应有效（不需要继续重试）
        返回 False 表示响应无效（需要继续重试）
        """
        if not text or not text.strip():
            return False  # 空文本无效，需要重试

        # 检查状态码 - 与_should_retry逻辑完全一致
        code = self._extract_status_code(text)
        if code is not None:
            if code in self.non_retryable_status_codes:
                logger.warning(f"检测到不可重试状态码 {code}，停止重试")
                return True  # 虽然有错误，但配置不允许重试，认为是"有效"结果
            if code in self.retryable_status_codes:
                logger.debug(f"检测到可重试状态码 {code}，继续重试")
                return False  # 需要继续重试

        # 检查错误关键词 - 与_should_retry逻辑一致
        text_lower = text.lower()
        for keyword in self.error_keywords:
            if keyword in text_lower:
                logger.debug(f"重试中仍检测到错误关键词 '{keyword}'，继续重试")
                return False  # 发现错误关键词，继续重试

        return True  # 没有发现问题，响应有效

    async def _apply_retry_delay(self, delay: float):
        """应用重试延时，增强错误处理"""
        try:
            if delay > 0:
                await asyncio.sleep(delay)
        except Exception as e:
            logger.warning(f"重试延时失败: {e}")

    def _handle_retry_failure(self, event: AstrMessageEvent):
        """处理重试失败的情况"""
        if self.fallback_reply and self.fallback_reply.strip():
            event.set_result(event.plain_result(self.fallback_reply.strip()))
        else:
            event.clear_result()
            event.stop_event()

    async def terminate(self):
        """插件卸载时的清理工作"""
        logger.info("已卸载 [IntelligentRetry] 插件 v2.7.2 (关键修复版)。")

# --- END OF FILE main.py ---
