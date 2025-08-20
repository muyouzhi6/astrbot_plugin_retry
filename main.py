import asyncio
import json
import re
from typing import Optional, Set, List, Dict, Any, Tuple

# AstrBot 运行环境导入；若在本地无框架，使用轻量兼容桩以便导入通过
try:
    from astrbot.api.event import AstrMessageEvent, filter
    from astrbot.api.star import Context, Star, register
    from astrbot.api import logger
    import astrbot.api.message_components as Comp
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
    
    class logger:
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

@register(
    "astrabot_plugin_retry",
    "木有知 & 长安某",
    "当LLM回复为空或包含特定错误关键词时，自动进行多次重试，保持完整上下文和人设。激进截断检测v4.4 - 用户可控",
    "4.4"
)
class IntelligentRetry(Star):
    """
    一个AstrBot插件，在检测到LLM回复为空或返回包含特定关键词的错误文本时，
    自动进行多次重试，并完整保持原有的上下文和人设。
    
    v4.4: 用户可控版本 - 解决用户三大问题 + 自由选择
    - 🚀 完美解决：错误检测不全、延迟太久、兜底词失效
    - �️ 用户可控：截断检测可开关，满足不同使用习惯
    - ⚡ 极速响应：0.2-0.5秒智能延迟，告别长等待
    - 🎯 精确检测：针对具体错误量身定制关键词
    - 💬 可靠兜底：多重机制确保自定义回复生效
    """

    def __init__(self, context: Context):
        super().__init__(context)
        
        # 从配置系统读取用户设置
        try:
            # 🎛️ 从AstrBot配置系统读取用户设置
            config = getattr(context, 'config_helper', None)
            if config and hasattr(config, 'get_plugin_config'):
                config_data = config.get_plugin_config()
            else:
                config_data = {}
        except:
            config_data = {}
        
        # 🎛️ 用户可配置选项 (从配置界面读取)
        self.enable_truncation_detection = config_data.get('enable_truncation_detection', True)
        self.enable_error_keyword_detection = config_data.get('enable_error_keyword_detection', True) 
        self.adaptive_delay = config_data.get('adaptive_delay', True)
        
        # 基础配置
        self.max_attempts = config_data.get('max_attempts', 3)
        self.retry_delay = config_data.get('retry_delay', 0.3)  # 极速响应：0.3秒基础延迟

        # 🔥 问题1解决：全面错误检测，精确匹配用户遇到的错误 
        # 从配置读取错误关键词，与_conf_schema.json保持一致
        schema_default_keywords = """api 返回的内容为空
API 返回的内容为空
APITimeoutError
错误类型: Exception
API 返回的 completion 由于内容安全过滤被拒绝(非 AstrBot)
语音转换失败，请稍后再试
语音转换失败
网络连接超时
服务器暂时不可用
请求频率过高
连接失败"""
        
        error_keywords_text = config_data.get('error_keywords', schema_default_keywords)
        self.error_keywords = [
            keyword.strip() 
            for keyword in error_keywords_text.split('\n') 
            if keyword.strip()
        ]

        # 人设控制
        self.always_use_system_prompt = True
        self.fallback_system_prompt_text = ""

        # 状态码配置 
        self.retryable_status_codes = {429, 500, 502, 503, 504}
        self.non_retryable_status_codes = {400, 401, 403, 404}

        # 调试配置
        self.log_context_preview = False
        self.context_preview_last_n = 3
        self.context_preview_max_chars = 120

        # 兜底回复 - 与_conf_schema.json保持一致
        schema_default_fallback = "抱歉，刚才遇到服务波动，我已自动为你重试多次仍未成功。请稍后再试或换个说法。"
        self.fallback_reply = config_data.get('fallback_reply', schema_default_fallback)

        # 其他配置
        self.always_use_system_prompt = config_data.get('always_use_system_prompt', True)
        self.fallback_system_prompt_text = config_data.get('fallback_system_prompt', "")

        # 状态码配置 
        self.retryable_status_codes = self._parse_codes(config_data.get('retryable_status_codes', '429\n500\n502\n503\n504'))
        self.non_retryable_status_codes = self._parse_codes(config_data.get('non_retryable_status_codes', '400\n401\n403\n404'))

        # 调试配置
        self.log_context_preview = config_data.get('log_context_preview', False)
        self.context_preview_last_n = config_data.get('context_preview_last_n', 3)
        self.context_preview_max_chars = config_data.get('context_preview_max_chars', 120)

        print(f"[重试插件] ⚡ 已加载 v4.4 智能控制版本，最多重试 {self.max_attempts} 次，{self.retry_delay}秒急速响应")
        print(f"[重试插件] 🎯 强化错误检测，精确捕获用户遇到的timeout错误")
        print(f"[重试插件] 🎛️ 截断检测: {'✅启用' if self.enable_truncation_detection else '❌禁用'} | 错误检测: {'✅启用' if self.enable_error_keyword_detection else '❌禁用'}")
        print(f"[重试插件] 💬 兜底回复: '{self.fallback_reply[:30]}...'")

    def _parse_codes(self, codes_str: str) -> Set[int]:
        """解析状态码配置"""
        codes = set()
        for line in codes_str.split('\n'):
            line = line.strip()
            if line.isdigit():
                code = int(line)
                if 400 <= code <= 599:
                    codes.add(code)
        return codes

    async def _get_complete_context(self, unified_msg_origin: str) -> List[Dict[str, Any]]:
        """获取完整的对话上下文"""
        if not unified_msg_origin:
            return []
            
        try:
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(unified_msg_origin)
            if not curr_cid:
                return []
            
            conv = await self.context.conversation_manager.get_conversation(unified_msg_origin, curr_cid)
            if not conv or not conv.history:
                return []
            
            context_history = json.loads(conv.history)
            return context_history if isinstance(context_history, list) else []
            
        except Exception as e:
            print(f"[重试插件] 获取对话上下文失败: {e}")
            return []

    async def _get_provider_config(self) -> Tuple[Optional[Any], Optional[str], Optional[Any]]:
        """获取 LLM 提供商的完整配置"""
        provider = self.context.get_using_provider()
        if not provider:
            return None, None, None
        
        # 获取系统提示词
        system_prompt = None
        try:
            if hasattr(provider, "system_prompt"):
                system_prompt = provider.system_prompt
            elif hasattr(provider, "config") and provider.config:
                system_prompt = provider.config.get("system_prompt")
        except Exception:
            pass
        
        # 获取工具配置
        func_tool = None
        try:
            if hasattr(provider, "func_tool"):
                func_tool = provider.func_tool
        except Exception:
            pass
        
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
            pass
            
        return has_system, sys_preview

    def _filter_system_messages(self, context_history: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
        """过滤掉上下文中的system消息"""
        filtered = []
        removed = 0
        
        for msg in context_history:
            if isinstance(msg, dict) and str(msg.get('role', '')).lower() == 'system':
                removed += 1
            else:
                filtered.append(msg)
                
        return filtered, removed

    def _set_fallback_response(self, response) -> None:
        """设置兜底回复"""
        try:
            # 使用兼容性方式创建Plain组件
            try:
                from astrbot.api.message_components import Plain
                print("[重试插件] 成功导入Plain组件")
            except Exception as import_error:
                print(f"[重试插件] Plain组件导入失败: {import_error}")
                # 兼容模式：创建简单的Plain类
                class Plain:
                    def __init__(self, text):
                        self.text = text
                        self.type = 'Plain'
                        self.convert = True
                print("[重试插件] 使用兼容Plain组件")
            
            # 创建兜底回复的消息组件
            fallback_plain = Plain(text=self.fallback_reply)
            
            # 尝试多种方式设置回复
            if hasattr(response, 'result_chain') and hasattr(response.result_chain, 'chain'):
                # 方式1：直接替换chain
                response.result_chain.chain = [fallback_plain]
                print(f"[重试插件] ✅ 方式1成功设置兜底回复: '{self.fallback_reply[:30]}...'")
            elif hasattr(response, 'result_chain'):
                # 方式2：创建新的MessageChain
                fallback_chain = type('MessageChain', (), {
                    'chain': [fallback_plain]
                })()
                response.result_chain = fallback_chain
                print(f"[重试插件] ✅ 方式2成功设置兜底回复: '{self.fallback_reply[:30]}...'")
            else:
                print("[重试插件] ⚠️ 无法设置兜底回复：response格式不支持")
                print(f"[重试插件] response类型: {type(response)}")
                print(f"[重试插件] response属性: {dir(response)}")
                
        except Exception as e:
            print(f"[重试插件] ❌ 设置兜底回复失败: {e}")
            import traceback
            print(f"[重试插件] 详细错误: {traceback.format_exc()}")

    async def _perform_retry_with_context(self, event: Any) -> Optional[Any]:
        """执行重试，完整保持原有上下文和人设"""
        provider, system_prompt, func_tool = await self._get_provider_config()
        
        if not provider:
            print("[重试插件] LLM提供商未启用，无法重试")
            return None

        try:
            # 获取完整的对话上下文
            context_history = await self._get_complete_context(event.unified_msg_origin)
            
            # 判断上下文中是否已经包含 system 消息
            has_system_in_contexts, sys_preview = self._extract_context_system_info(context_history)
            
            # 获取图片URL
            image_urls = []
            try:
                for comp in event.message_obj.message:
                    if hasattr(comp, "url") and comp.url:
                        image_urls.append(comp.url)
            except Exception:
                pass

            print(f"[重试插件] 正在重试... 上下文长度: {len(context_history)}")

            # 可选：输出上下文预览
            if self.log_context_preview and context_history and self.context_preview_last_n > 0:
                try:
                    tail = context_history[-self.context_preview_last_n:]
                    preview_lines = []
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
                        preview_lines.append(f"#{idx} [{role}] {text}")
                    print(f"[重试插件] 上下文预览:\n" + "\n".join(preview_lines))
                except Exception:
                    pass

            # 处理强制人设覆盖逻辑
            if self.always_use_system_prompt:
                # 若 Provider 无人设而插件提供了备用人设，则使用备用人设
                if not system_prompt and self.fallback_system_prompt_text:
                    system_prompt = self.fallback_system_prompt_text
                    print("[重试插件] 使用备用人设")

                if system_prompt:
                    # 移除上下文中的所有 system 消息
                    context_history, removed = self._filter_system_messages(context_history)
                    if removed > 0:
                        print(f"[重试插件] 已强制覆盖人设：移除 {removed} 条历史 system 消息")
                    # 更新标记
                    has_system_in_contexts = False
            
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

            # 执行LLM调用
            if not provider:
                print("[重试插件] Provider在重试过程中不可用")
                return None
                
            llm_response = await provider.text_chat(**kwargs)
            return llm_response
            
        except Exception as e:
            print(f"[重试插件] 重试调用LLM时发生错误: {e}")
            return None

    def _detect_truncation(self, text: str, llm_response=None) -> bool:
        """
        激进截断检测 v4.1 - 彻底解决"巧合截断"问题
        """
        if not text:
            return True  # 空回复肯定是问题
        
        # 第一优先级：API层检测
        if llm_response:
            try:
                if hasattr(llm_response, 'choices') and llm_response.choices:
                    finish_reason = getattr(llm_response.choices[0], 'finish_reason', None)
                    if finish_reason == 'length':
                        print("[重试插件] 检测到finish_reason='length'，官方确认截断")
                        return True
            except Exception:
                pass
        
        # 第二优先级：明显截断检测
        text = text.strip()
        
        # 特殊情况：明显的列表截断
        if re.search(r'\d+\.\s*$', text):  # "2." 或 "2. " 结尾
            return True
        
        # 特殊情况：明显的序号截断  
        if re.search(r'[（(]\d+[）)]\s*$', text):  # "(2)" 或 "（2）" 结尾
            return True
        
        # 第三优先级：激进检测 - 只有明确完整的才不重试
        return not self._is_clearly_complete(text)

    def _is_clearly_complete(self, text: str) -> bool:
        """
        明确完整检测 - 只识别绝对确定完整的情况
        """
        if not text or not text.strip():
            return False
        
        text = text.strip()
        
        # 明确的完整结束信号
        
        # 1. 句号结尾 = 绝对完整
        if text.endswith(('.', '。', '！', '!', '？', '?')):
            return True
        
        # 2. 省略号/分号 = 思考完整 
        if text.endswith(('…', ';', '；')):
            return True
        
        # 3. 引号结尾 = 对话完整
        if text.endswith(('"', '"', "'")):
            return True
        
        # 4. 括号结尾 = 补充完整
        if text.endswith((')', '）', ']', '】', '}', '》')):
            return True
        
        # 5. 代码块结尾 = 代码完整
        if text.endswith('```'):
            return True
        
        # 6. 文件/链接 = 资源完整
        if re.search(r'\.(com|org|net|edu|gov|cn|co\.uk|json|txt|py|js|html|css|md|pdf|doc|xlsx)$', text):
            return True
        
        # 7. 版本号 = 版本完整
        if re.search(r'v?\d+\.\d+(\.\d+)?$', text):
            return True
        
        # 8. 百分比 = 数据完整
        if re.search(r'\d+%$', text):
            return True
        
        # 9. 明确的数值+单位 = 度量完整
        if re.search(r'\d+(\.\d+)?\s*(GB|MB|KB|TB|元|块|个|次|秒|分钟|小时|天|年|月|kg|g|m|cm|km)$', text):
            return True
        
        # 10. "完成"类词汇 = 任务完整
        completion_words = ['完成', '结束', '搞定', '好的', '明白', '了解', '收到', '明白了', '知道了', '完成了', '结束了', 'done', 'finished', 'complete', 'ok', 'got it']
        for word in completion_words:
            if text.endswith(word):
                return True
        
        # 10.5. 更灵活的完成词汇检测（不只是结尾）
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
            if re.search(pattern, text) and len(text) <= 10:  # 短句中包含完成词汇
                return True
        
        # 11. 完整的句子结构（保守判断）
        # 更好的词汇分割方式
        words = re.findall(r'[\u4e00-\u9fff]+|[a-zA-Z]+', text)  # 分别匹配中文和英文
        if len(words) >= 3:  # 至少3个词组才考虑为完整句子
            last_word = words[-1] if words else ""
            # 排除明显的连接词
            if len(last_word) >= 1 and not last_word.lower() in [
                '但是', '然后', '所以', '而且', '另外', '因此', '于是', '接着', '包括', '如下', '还有', '以及',
                'however', 'therefore', 'moreover', 'furthermore', 'because', 'since', 'including'
            ]:
                # 包含肯定性/完整性词汇的句子，可能是完整的
                completeness_indicators = [
                    '是', '有', '会', '能', '可以', '应该', '需要', '正常', '成功', '完整', '回复', '内容', 
                    '没有', '问题', '这是', '这个', '一个', '已经', '现在', '可能', '应该', '确实',
                    '知道', '明白', '理解', '认为', '觉得', '发现', '注意', '建议', '推荐', '希望'
                ]
                # 需要同时满足：有完整性词汇 + 句子长度合理（避免过短判断错误）
                has_completeness = any(pattern in text for pattern in completeness_indicators)
                is_reasonable_length = len(text) >= 6  # 至少6个字符
                
                if has_completeness and is_reasonable_length:
                    return True
        
        # 12. 常见的完整表达模式
        complete_patterns = [
            r'^正常的.{2,}$',      # "正常的xxx"
            r'^这是.{4,}[^一个的]$',  # "这是xxx" 但排除"这是一个"、"这是一个的"等明显截断
            r'^我.{2,}了$',        # "我xxx了"
            r'^.{3,}内容$',        # "xxx内容"
            r'^.{3,}没有问题$',     # "xxx没有问题"
            r'^.{2,}很好$',        # "xxx很好"
            r'^.{2,}不错$',        # "xxx不错"
        ]
        
        for pattern in complete_patterns:
            if re.match(pattern, text):
                return True
        
        # 其他情况默认为"可能截断"，激进重试
        return False

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response) -> bool:
        """
        处理LLM响应，检测并重试无效回复
        """
        try:
            # 只处理LLM响应阶段
            if not response:
                return True
            
            # 提取回复文本
            reply_text = ""
            if hasattr(response, 'result_chain') and response.result_chain:
                try:
                    for comp in response.result_chain.chain:
                        if hasattr(comp, 'text') and comp.text:
                            reply_text += comp.text
                except:
                    pass
            
            # 提取原始completion
            raw_completion = getattr(response, 'raw_completion', None)
            
            # 检查是否需要重试
            should_retry = self._should_retry_simple(reply_text, raw_completion)
            
            if should_retry:
                print(f"[重试插件] 🔄 检测到无效回复，准备重试: '{reply_text[:50]}...'")
                
                # 多次重试逻辑
                for attempt in range(1, self.max_attempts + 1):
                    print(f"[重试插件] 第 {attempt}/{self.max_attempts} 次重试...")
                    
                    # 🔥 问题2解决：优化延迟策略，快速响应
                    if attempt > 1:
                        if self.adaptive_delay:
                            # 自适应延迟：基于错误类型智能调整
                            if "timeout" in reply_text.lower() or "超时" in reply_text:
                                delay = 0.2  # 超时错误快速重试
                            elif "请求失败" in reply_text:
                                delay = 0.3  # 网络错误稍微延迟
                            else:
                                delay = 0.5  # 其他错误正常延迟
                        else:
                            # 传统指数延迟
                            delay = min(0.5 * attempt, 2.0)
                        
                        print(f"[重试插件] ⚡ 智能延迟 {delay} 秒后重试...")
                        await asyncio.sleep(delay)
                    
                    # 执行重试
                    retry_result = await self._perform_retry_with_context(event)
                    
                    if not retry_result:
                        print(f"[重试插件] 第 {attempt} 次重试调用失败")
                        continue
                    
                    # 验证重试结果是否真的有效
                    retry_text = ""
                    if hasattr(retry_result, 'result_chain') and retry_result.result_chain:
                        try:
                            for comp in retry_result.result_chain.chain:
                                if hasattr(comp, 'text') and comp.text:
                                    retry_text += comp.text
                        except:
                            pass
                    
                    # 检查重试结果是否还是无效的
                    retry_raw_completion = getattr(retry_result, 'raw_completion', None)
                    is_retry_still_invalid = self._should_retry_simple(retry_text, retry_raw_completion)
                    
                    if is_retry_still_invalid:
                        print(f"[重试插件] 第 {attempt} 次重试仍然无效: '{retry_text[:50]}...' (长度:{len(retry_text)})")
                        if attempt < self.max_attempts:
                            # 延迟已在循环开始处理，这里不需要额外延迟
                            continue
                        else:
                            print(f"[重试插件] ❌ 已达到最大重试次数 ({self.max_attempts})，全部重试失败")
                            # 使用兜底回复替换无效response
                            self._set_fallback_response(response)
                            break
                    else:
                        print(f"[重试插件] ✅ 第 {attempt} 次重试真正成功: '{retry_text[:50]}...' (长度:{len(retry_text)})")
                        # 替换response内容
                        if hasattr(retry_result, 'result_chain'):
                            response.result_chain = retry_result.result_chain
                        if hasattr(retry_result, 'raw_completion'):
                            response.raw_completion = retry_result.raw_completion
                        break
            
            return True
            
        except Exception as e:
            print(f"[重试插件] 错误: {e}")
            return True

    def _should_retry_simple(self, text: str, llm_response=None) -> bool:
        """
        简化的重试判断逻辑 - 支持用户配置开关
        """
        # 1. 空回复检查（始终启用）
        if not text or not text.strip():
            print("[重试插件] 检测到空回复")
            return True
        
        # 2. 错误关键词检查（可配置，但建议保持开启）
        if self.enable_error_keyword_detection:
            text_lower = text.lower().strip()
            
            # 精确匹配您遇到的具体错误
            specific_errors = [
                "请求失败。错误类型",
                "APTL错误信息",
                "request time out请在控制台查看",
                "分享错误详情"
            ]
            
            for error in specific_errors:
                if error.lower() in text_lower:
                    print(f"[重试插件] 🎯 检测到特定错误模式: {error}")
                    return True
            
            # 通用关键词检查
            for keyword in self.error_keywords:
                if keyword.lower() in text_lower:
                    print(f"[重试插件] 🔍 检测到错误关键词: {keyword}")
                    return True
            
            # 正则表达式模式检查
            error_patterns = [
                r'错误类型.*?exception',  # 错误类型相关
                r'请求.*?失败',           # 请求失败
                r'time.*?out',           # 超时相关
                r'错误.*?详情',           # 错误详情
                r'控制台.*?查看',         # 控制台查看
            ]
            
            for pattern in error_patterns:
                if re.search(pattern, text_lower):
                    print(f"[重试插件] 📋 检测到错误模式: {pattern}")
                    return True
        
        # 3. 截断检测（用户可配置开关）
        if self.enable_truncation_detection:
            if self._detect_truncation(text, llm_response):
                print("[重试插件] 🔄 检测到回复截断")
                return True
        else:
            print("[重试插件] 🎛️ 截断检测已禁用，跳过截断检查")
        
        return False
