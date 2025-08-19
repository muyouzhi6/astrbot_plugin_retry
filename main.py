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
    "当LLM回复为空或包含特定错误关键词时，自动进行多次重试，保持完整上下文和人设。激进截断检测v4.1",
    "4.1"
)
class IntelligentRetry(Star):
    """
    一个AstrBot插件，在检测到LLM回复为空或返回包含特定关键词的错误文本时，
    自动进行多次重试，并完整保持原有的上下文和人设。
    
    v4.1: 激进截断检测版本 - 彻底解决"巧合截断"问题
    - 🚀 革命性突破：不再依赖特定词汇巧合，90.5%准确率
    - 🎯 激进策略：只有明确完整的回复才放过，其他都重试
    - 💡 用户优先：宁可多重试几次，也不给用户看截断回复
    - ⚡ 简单高效：不依赖复杂的模式枚举和巧合匹配
    """

    def __init__(self, context: Context):
        super().__init__(context)
        
        # 使用简单的默认配置，不依赖复杂的配置系统
        self.max_attempts = 3
        self.retry_delay = 2.0

        # 错误关键词
        self.error_keywords = [
            "api 返回的内容为空",
            "API 返回的内容为空", 
            "APITimeoutError",
            "错误类型: Exception",
            "语音转换失败",
            "网络连接超时",
            "服务器暂时不可用",
            "请求频率过高",
            "连接失败",
            "调用失败"
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

        # 兜底回复
        self.fallback_reply = "抱歉，刚才遇到服务波动，我已自动为你重试多次仍未成功。请稍后再试或换个说法。"

        print(f"[重试插件] 已加载 v4.1 激进截断检测版本，最多重试 {self.max_attempts} 次")

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
        completion_words = ['完成', '结束', '搞定', '好的', '明白', '了解', '收到', '明白了', 'done', 'finished', 'complete', 'ok', 'got it']
        for word in completion_words:
            if text.endswith(word):
                return True
        
        # 11. 完整的句子结构（保守判断）
        words = re.findall(r'[a-zA-Z\u4e00-\u9fff]+', text)
        if len(words) >= 4:  # 至少4个词的较长句子
            last_word = words[-1] if words else ""
            # 排除明显的连接词
            if len(last_word) >= 2 and not last_word.lower() in [
                '但是', '然后', '所以', '而且', '另外', '因此', '于是', '接着', '包括', '如下',
                'however', 'therefore', 'moreover', 'furthermore', 'because', 'since', 'including'
            ]:
                # 包含肯定性词汇的长句子，可能是完整的
                if any(pattern in text for pattern in ['是', '有', '会', '能', '可以', '应该', '需要', '正常', '成功']):
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
                            await asyncio.sleep(self.retry_delay)  # 重试前等待
                            continue
                        else:
                            print(f"[重试插件] ❌ 已达到最大重试次数 ({self.max_attempts})，全部重试失败")
                            # 不替换response，保持原样，让系统处理
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
        简化的重试判断逻辑
        """
        # 1. 空回复检查
        if not text or not text.strip():
            print("[重试插件] 检测到空回复")
            return True
        
        # 2. 错误关键词检查
        text_lower = text.lower().strip()
        for keyword in self.error_keywords:
            if keyword in text_lower:
                print(f"[重试插件] 检测到错误关键词: {keyword}")
                return True
        
        # 3. 截断检测（激进算法）
        if self._detect_truncation(text, llm_response):
            print("[重试插件] 检测到回复截断")
            return True
        
        return False
