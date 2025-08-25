# --- START OF FILE main.py ---

import asyncio
import json
import re
import time
from typing import Dict, Any, Optional

import astrbot.api.message_components as Comp
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import ProviderRequest


@register(
    "intelligent_retry",
    "木有知 & 长安某 (优化增强版)",
    "当LLM回复为空或包含特定错误关键词时，自动进行多次重试，使用原始请求参数确保完整重试。新增智能截断检测与并发重试功能，简化架构提升性能，修复截断检测准确性",
    "2.9.3"
)
class IntelligentRetry(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        
        # 请求存储（借鉴v2版本的设计）
        self.pending_requests: Dict[str, Dict[str, Any]] = {}
        
        # 解析配置
        self._parse_config(config)
        
        logger.info(
            f"已加载 [IntelligentRetry] 插件 v2.9.3, "
            f"最大重试次数: {self.max_attempts}, 截断检测: {'启用' if self.enable_truncation_retry else '禁用'}, "
            f"并发重试: {'启用' if self.enable_concurrent_retry else '禁用'}"
        )
    
    def _parse_config(self, config: AstrBotConfig) -> None:
        """解析配置文件，统一配置初始化逻辑"""
        # 基础配置
        self.max_attempts = config.get('max_attempts', 3)
        self.retry_delay = config.get('retry_delay', 2)
        self.retry_delay_mode = config.get('retry_delay_mode', 'exponential').lower().strip()
        
        # 错误关键词配置
        default_keywords = "api 返回的内容为空\nAPI 返回的 completion 由于内容安全过滤被拒绝(非 AstrBot)\n调用失败"
        keywords_str = config.get('error_keywords', default_keywords)
        self.error_keywords = [k.strip().lower() for k in keywords_str.split('\n') if k.strip()]

        # 基于状态码的重试控制
        self.retryable_status_codes = self._parse_status_codes(config.get('retryable_status_codes', "400\n429\n502\n503\n504"))
        self.non_retryable_status_codes = self._parse_status_codes(config.get('non_retryable_status_codes', ""))

        # 兜底回复
        self.fallback_reply = config.get('fallback_reply', "抱歉，刚才遇到服务波动，我已自动为你重试多次仍未成功。请稍后再试或换个说法。")

        # 截断重试配置
        self.enable_truncation_retry = bool(config.get('enable_truncation_retry', False))
        
        # 新增：截断检测模式和选项
        self.truncation_detection_mode = config.get('truncation_detection_mode', 'enhanced').lower().strip()
        self.check_structural_integrity = bool(config.get('check_structural_integrity', True))
        self.check_content_type_specific = bool(config.get('check_content_type_specific', True))
        self.min_reasonable_length = max(5, int(config.get('min_reasonable_length', 10)))
        self.code_block_detection = bool(config.get('code_block_detection', True))
        self.quote_matching_detection = bool(config.get('quote_matching_detection', True))
        
        # 原有的正则表达式配置（保持向后兼容）
        self.truncation_valid_tail_pattern = config.get(
            'truncation_valid_tail_pattern',
            r"[。！？!?,;:、，．…—\-\(\)\[\]'\"""''\\w\\d_\u4e00-\u9fa5\\s\\t]$"
            r"|\.(com|cn|org|net|io|ai|pdf|jpg|png|jpeg|gif|mp3|mp4|txt|zip|tar|gz|html|htm)$"
            r"|https?://[\\w\.-]+$"
        )
        
        # 并发重试配置
        self.enable_concurrent_retry = bool(config.get('enable_concurrent_retry', False))
        self.concurrent_retry_threshold = max(1, int(config.get('concurrent_retry_threshold', 1)))
        self.concurrent_retry_count = max(1, min(5, int(config.get('concurrent_retry_count', 2))))  # 限制1-5个
        self.concurrent_retry_timeout = max(10, int(config.get('concurrent_retry_timeout', 30)))
    
    def _parse_status_codes(self, codes_str: str) -> set:
        """解析状态码配置字符串"""
        codes = set()
        for line in codes_str.split('\n'):
            line = line.strip()
            if line.isdigit():
                try:
                    codes.add(int(line))
                except Exception:
                    pass
        return codes

    def _get_request_key(self, event: AstrMessageEvent) -> str:
        """生成稳定的请求标识符（基于事件属性，非时间）"""
        platform = getattr(event.unified_msg_origin, 'platform', 'unknown')
        sender_id = str(getattr(event.unified_msg_origin, 'sender_id', 'unknown'))
        msg_type = getattr(event.unified_msg_origin, 'msg_type', 'unknown')
        
        # 使用事件固有属性，确保两个钩子使用相同的键
        key = f"{platform}:{msg_type}:{sender_id}:{hash(event.message_str)}"
        return key

    @filter.on_llm_request()
    async def store_llm_request(self, event: AstrMessageEvent, req: ProviderRequest, *args):
        """存储LLM请求参数（借鉴v2版本的双钩子机制）"""
        # 钩子执行追踪
        logger.debug(f"[HOOK] on_llm_request 触发 - 消息: '{event.message_str[:50]}...'")
        logger.debug(f"[HOOK] Event ID: {id(event)}, 类型: {type(event)}")
        
        request_key = self._get_request_key(event)
        
        # 获取图片URL
        image_urls = [
            comp.url for comp in event.message_obj.message
            if isinstance(comp, Comp.Image) and hasattr(comp, "url") and comp.url
        ]
        
        # 存储请求参数
        self.pending_requests[request_key] = {
            'prompt': event.message_str,
            'contexts': getattr(req, 'contexts', []),
            'image_urls': image_urls,
            'system_prompt': getattr(req, 'system_prompt', ''),
            'func_tool': getattr(req, 'func_tool', None),
            'unified_msg_origin': event.unified_msg_origin
        }

    def _is_truncated(self, text: str) -> bool:
        """主入口方法：多层截断检测"""
        if not text or not text.strip():
            logger.debug("截断检测: 文本为空，不是截断")
            return False
        
        # 如果内容太短，一般不认为是截断（除非明显不完整）
        if len(text.strip()) < self.min_reasonable_length:
            logger.debug(f"截断检测: 文本长度 {len(text.strip())} < {self.min_reasonable_length}，可能太短")
            return False
        
        try:
            logger.debug(f"🔍 开始截断检测 - 模式: {self.truncation_detection_mode}, 文本长度: {len(text)}")
            logger.debug(f"文本结尾50字符: '{text[-50:]}'")
            
            # 根据检测模式选择策略
            if self.truncation_detection_mode == 'basic':
                result = self._detect_character_level_truncation(text)
                logger.debug(f"基础模式检测结果: {result}")
                return result
            elif self.truncation_detection_mode == 'enhanced':
                # 多层检测：字符级 + 结构级 + 内容类型
                char_result = self._detect_character_level_truncation(text)
                struct_result = self._detect_structural_truncation(text)
                content_result = self._detect_content_type_truncation(text)
                
                final_result = char_result or struct_result or content_result
                logger.debug(f"增强模式检测结果: 字符级={char_result}, 结构级={struct_result}, 内容类型={content_result}, 最终={final_result}")
                return final_result
            elif self.truncation_detection_mode == 'strict':
                # 严格模式：所有检测都要通过
                char_result = self._detect_character_level_truncation(text)
                struct_result = self._detect_structural_truncation(text)
                final_result = char_result and struct_result
                logger.debug(f"严格模式检测结果: 字符级={char_result}, 结构级={struct_result}, 最终={final_result}")
                return final_result
            else:
                # 默认使用基础模式
                result = self._detect_character_level_truncation(text)
                logger.debug(f"默认模式检测结果: {result}")
                return result
        except Exception as e:
            logger.warning(f"截断检测发生错误，回退到基础模式: {e}")
            return self._detect_character_level_truncation(text)
    
    def _detect_character_level_truncation(self, text: str) -> bool:
        """第一层：改进的字符级截断检测"""
        if not text or not text.strip():
            return False
        
        # 获取最后一行，去除首尾空白
        last_line = text.strip().splitlines()[-1].strip()
        logger.debug(f"📝 字符级检测 - 最后一行: '{last_line}'")
        
        # 1. 检查是否以句子结束标点符号结尾（中英文）
        sentence_endings = r"[。！？!?\.;。？！]$"
        if re.search(sentence_endings, last_line):
            logger.debug("✅ 字符级检测: 以句子结束标点结尾，不是截断")
            return False  # 以句子结束标点结尾，不是截断
        
        # 2. 检查是否以明确的完整结构结尾
        complete_endings = [
            r"[)]$",  # 右括号
            r"[}]$",  # 右大括号
            r"[]]$",  # 右方括号
            r"[>]$",  # 右尖括号
            r'["\']$',  # 引号结尾
            r"\d+[%°]?$",  # 数字加可选的百分号或度数符号
            r"\.(com|cn|org|net|io|ai|pdf|jpg|png|jpeg|gif|mp3|mp4|txt|zip|tar|gz|html|htm)$",  # 文件扩展名
            r"https?://[\w\.-]+/?$",  # URL
        ]
        
        for i, pattern in enumerate(complete_endings):
            if re.search(pattern, last_line, re.IGNORECASE):
                logger.debug(f"✅ 字符级检测: 匹配完整结构模式 #{i+1}，不是截断")
                return False  # 以完整结构结尾，不是截断
        
        # 3. 检查是否以明显的截断标志结尾
        truncation_indicators = [
            r"[，,、；;:]$",  # 逗号、顿号、分号、冒号（通常表示还有后续内容）
            r"(而且|但是|然而|因此|所以|不过|另外|此外|同时|另一方面|另一种|还有)$",  # 中文连接词
            r"(and|or|but|however|therefore|moreover|furthermore|additionally|also|as well as)$",  # 英文连接词
            r"(的|了|在|对|为|与|及|以|从|由|向|到|于|等|或|和)$",  # 中文介词、连词
            r"(is|are|was|were|will|would|should|could|has|have|had)$",  # 英文助动词
            r"(a|an|the|this|that|these|those|my|your|his|her|its|our|their)$",  # 英文限定词
            r"\w+ing$",  # 英文现在分词
        ]
        
        for i, pattern in enumerate(truncation_indicators):
            if re.search(pattern, last_line, re.IGNORECASE):
                logger.debug(f"⚠️ 字符级检测: 匹配截断指示模式 #{i+1}，判定为截断")
                return True  # 以截断指示词结尾，可能被截断
        
        # 4. 检查文本长度和内容复杂度
        # 如果是很短的文本且没有明确的结束标志，可能是截断
        if len(last_line) < 5:
            logger.debug(f"⚠️ 字符级检测: 最后一行太短 ({len(last_line)} < 5)，判定为截断")
            return True
        
        # 5. 检查是否以普通字符结尾但没有上下文完整性
        # 如果最后的字符是字母或中文字符，但不是明确的结束
        if re.search(r"[\w\u4e00-\u9fa5]$", last_line):
            logger.debug("🤔 字符级检测: 以普通字符结尾，进行语义完整性检查...")
            # 进一步检查：如果整个句子看起来不完整，则认为是截断
            # 检查是否包含动词但没有宾语，或其他语法不完整的模式
            semantic_result = self._check_semantic_completeness(last_line)
            logger.debug(f"🤔 语义完整性检查结果: {semantic_result}")
            return semantic_result
        
        # 默认：如果无法确定，保守起见不认为是截断
        logger.debug("✅ 字符级检测: 无法确定，保守判定为不截断")
        return False
    
    def _check_semantic_completeness(self, text: str) -> bool:
        """检查语义完整性，判断句子是否完整"""
        logger.debug(f"🧠 语义检测 - 分析文本: '{text}'")
        
        # 检查是否以动词结尾但没有合适的补语
        chinese_verbs_needing_object = ['正在', '开始', '完成', '进行', '处理', '分析', '考虑', '思考', '学习', '研究', '制作', '创建', '设计', '编写', '开发']
        for verb in chinese_verbs_needing_object:
            if text.endswith(verb):
                logger.debug(f"🧠 语义检测: 以需要宾语的动词 '{verb}' 结尾，判定为截断")
                return True  # 这些动词后面通常需要宾语
        
        # 检查是否以介词结尾（通常需要宾语）
        prep_pattern = r'(对于|关于|通过|使用|基于|根据|按照|依据|针对|面对|围绕|涉及)$'
        if re.search(prep_pattern, text):
            logger.debug("🧠 语义检测: 以介词结尾，通常需要宾语，判定为截断")
            return True
        
        # 检查英文的不完整模式
        english_incomplete_pattern = r'(to|for|with|by|in|on|at|from|about|into|onto|upon|during|before|after)$'
        if re.search(english_incomplete_pattern, text, re.IGNORECASE):
            logger.debug("🧠 语义检测: 以英文介词结尾，判定为截断")
            return True
        
        # 新增：检查疑问词开头但没有问号结尾的情况
        question_starters = ['什么', '为什么', '怎么', '如何', '哪个', '哪些', '谁', '何时', '在哪', 'what', 'why', 'how', 'which', 'who', 'when', 'where']
        for starter in question_starters:
            if text.lower().startswith(starter.lower()) and not text.endswith('?') and not text.endswith('？'):
                logger.debug(f"🧠 语义检测: 以疑问词 '{starter}' 开头但没有问号结尾，可能是截断的问题")
                return True
        
        # 新增：检查列举但没有结束的情况
        list_indicators = ['包括', '有', '分别是', '例如', '比如', 'such as', 'including', 'like']
        for indicator in list_indicators:
            if indicator in text and text.endswith(indicator):
                logger.debug(f"🧠 语义检测: 以列举词 '{indicator}' 结尾，可能后续有内容")
                return True
        
        # 新增：检查定语从句但没有完整的情况
        relative_markers = ['其中', '其', '这个', '这些', '那个', '那些', 'which', 'that', 'who', 'whose', 'where']
        for marker in relative_markers:
            if text.endswith(marker):
                logger.debug(f"🧠 语义检测: 以关系词 '{marker}' 结尾，可能是不完整的定语从句")
                return True
        
        logger.debug("🧠 语义检测: 未发现明显的不完整标志")
        return False
    
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
            if self.code_block_detection and not self._check_markdown_completeness(text):
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
            
            if content_type == 'code':
                return self._is_code_truncated(text)
            elif content_type == 'list':
                return self._is_list_truncated(text)
            elif content_type == 'table':
                return self._is_table_truncated(text)
            elif content_type == 'json':
                return self._is_json_truncated(text)
            else:
                # 自然语言检测
                return self._is_natural_language_truncated(text)
        except Exception as e:
            logger.debug(f"内容类型检测出错，跳过: {e}")
            return False
    
    def _check_bracket_balance(self, text: str) -> bool:
        """检查括号是否平衡"""
        brackets = {'(': ')', '[': ']', '{': '}', '<': '>'}
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
        code_blocks = text.count('```')
        if code_blocks % 2 != 0:
            return False
        
        # 检查行内代码
        inline_code = text.count('`') - text.count('\\`')
        if inline_code % 2 != 0:
            return False
        
        return True
    
    def _get_content_type(self, text: str) -> str:
        """识别内容类型"""
        text_lower = text.lower().strip()
        
        # 代码检测
        if (text.count('```') >= 2 or 
            re.search(r'^\s*(def|function|class|import|from|#include)', text, re.MULTILINE) or
            text.count('{') > 2 and text.count('}') > 2):
            return 'code'
        
        # JSON检测
        if ((text_lower.startswith('{') and text_lower.endswith('}')) or
            (text_lower.startswith('[') and text_lower.endswith(']'))):
            return 'json'
        
        # 列表检测
        if re.search(r'^\s*[-*+]\s+', text, re.MULTILINE) or re.search(r'^\s*\d+\.\s+', text, re.MULTILINE):
            return 'list'
        
        # 表格检测
        if '|' in text and text.count('|') > 3:
            return 'table'
        
        return 'natural_language'
    
    def _is_code_truncated(self, text: str) -> bool:
        """检测代码是否被截断"""
        # 检查是否在字符串中间截断
        if text.endswith('"') is False and '"' in text and text.count('"') % 2 == 1:
            return True
        
        # 检查是否在注释中间截断
        lines = text.splitlines()
        if lines and lines[-1].strip().startswith('#') and not lines[-1].strip().endswith('.'):
            return True
        
        return False
    
    def _is_list_truncated(self, text: str) -> bool:
        """检测列表是否被截断"""
        lines = text.strip().splitlines()
        if not lines:
            return False
        
        last_line = lines[-1].strip()
        # 如果最后一行是未完成的列表项
        if (re.match(r'^\s*[-*+]\s*$', last_line) or 
            re.match(r'^\s*\d+\.\s*$', last_line)):
            return True
        
        return False
    
    def _is_table_truncated(self, text: str) -> bool:
        """检测表格是否被截断"""
        lines = text.strip().splitlines()
        if not lines:
            return False
        
        # 检查最后一行是否是不完整的表格行
        last_line = lines[-1]
        if '|' in last_line and not last_line.strip().endswith('|'):
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
        conjunctions = ['and', 'or', 'but', 'however', 'therefore', '而且', '但是', '然而', '因此', '所以']
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
        if hasattr(result, 'chain'):
            for comp in result.chain:
                # 任何非Plain类型的消息段都算作有内容
                if not isinstance(comp, Comp.Plain):
                    has_content = True
                    break
                # Plain类型但text非空
                if hasattr(comp, 'text') and str(comp.text).strip():
                    has_content = True
                    break
        
        if not has_content:
            logger.debug("检测到空回复，需要重试")
            return True
        
        # 检查错误关键词和状态码
        message_str = result.get_plain_text() if hasattr(result, 'get_plain_text') else ''
        if message_str:
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
            
            # 截断检测
            if self.enable_truncation_retry and self._is_truncated(message_str):
                logger.info(f"💥 检测到回复疑似被截断，触发截断重试")
                logger.debug(f"截断内容分析 - 全文长度: {len(message_str)}, 结尾内容: '{message_str[-50:]}'")
                return True
        
        return False

    async def _perform_retry_with_stored_params(self, request_key: str) -> Optional[Any]:
        """使用存储的参数执行重试（带备份恢复机制）"""
        # 获取存储的参数
        if request_key not in self.pending_requests:
            logger.error(f"未找到存储的请求参数: {request_key}")
            return None
            
        stored_params = self.pending_requests[request_key]
        
        provider = self.context.get_using_provider()
        
        if not provider:
            logger.warning("LLM提供商未启用，无法重试。")
            return None

        try:
            # 构建重试请求参数
            kwargs = {
                'prompt': stored_params['prompt'],
                'contexts': stored_params['contexts'],
                'image_urls': stored_params['image_urls'],
                'func_tool': stored_params['func_tool'],
            }
            
            # 使用存储的原始system_prompt
            if stored_params['system_prompt']:
                kwargs['system_prompt'] = stored_params['system_prompt']

            logger.debug(f"正在使用存储的参数进行重试... Prompt: '{stored_params['prompt']}'")
            
            llm_response = await provider.text_chat(**kwargs)
            return llm_response
            
        except Exception as e:
            logger.error(f"重试调用LLM时发生错误: {e}")
            return None

    async def _execute_retry_sequence(self, event: AstrMessageEvent, request_key: str) -> bool:
        """执行重试序列（支持顺序和并发两种模式）"""
        delay = max(0, int(self.retry_delay))
        
        # 如果未启用并发重试，使用原有的顺序重试逻辑
        if not self.enable_concurrent_retry:
            return await self._sequential_retry_sequence(event, request_key, self.max_attempts, delay)
        
        # 并发重试模式：根据阈值决定是否跳过顺序重试
        logger.debug(f"[DEBUG] 并发重试检查: enable={self.enable_concurrent_retry}, threshold={self.concurrent_retry_threshold}")
        
        if self.concurrent_retry_threshold == 0:
            # 阈值为0：直接启用并发重试，使用全部重试次数
            logger.info("阈值=0，强制启动直接并发重试模式，跳过顺序重试阶段")
            return await self._concurrent_retry_sequence(event, request_key, self.max_attempts)
        
        # 混合重试模式：先顺序重试到阈值，然后并发重试
        sequential_attempts = min(self.concurrent_retry_threshold, self.max_attempts)
        logger.info(f"混合重试模式：先 {sequential_attempts} 次顺序，后并发")
        
        # 第一阶段：顺序重试
        if sequential_attempts > 0:
            logger.debug(f"开始顺序重试阶段（{sequential_attempts} 次）")
            sequential_success = await self._sequential_retry_sequence(event, request_key, sequential_attempts, delay)
            if sequential_success:
                return True
        
        # 第二阶段：并发重试（如果还有剩余尝试次数）
        remaining_attempts = self.max_attempts - sequential_attempts
        if remaining_attempts > 0:
            logger.debug(f"顺序重试失败，切换到并发重试阶段（剩余 {remaining_attempts} 次）")
            return await self._concurrent_retry_sequence(event, request_key, remaining_attempts)
        
        return False
    
    async def _sequential_retry_sequence(self, event: AstrMessageEvent, request_key: str, max_attempts: int, initial_delay: int) -> bool:
        """顺序重试序列（从原_execute_retry_sequence方法拆分出来）"""
        delay = initial_delay
        
        for attempt in range(1, max_attempts + 1):
            logger.info(f"第 {attempt}/{max_attempts} 次重试...")

            new_response = await self._perform_retry_with_stored_params(request_key)

            if not new_response or not getattr(new_response, 'completion_text', ''):
                logger.warning(f"第 {attempt} 次重试返回空结果")
            else:
                new_text = new_response.completion_text.strip()
                
                # 检查新回复是否包含错误
                new_text_lower = new_text.lower()
                has_error = any(keyword in new_text_lower for keyword in self.error_keywords)
                
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
                    event.set_result(event.plain_result(new_text))
                    return True
                else:
                    logger.warning(f"第 {attempt} 次重试仍包含错误或为空: {new_text[:100]}...")

            # 等待后重试
            if attempt < max_attempts and delay > 0:
                await asyncio.sleep(delay)
                if self.retry_delay_mode == 'exponential':
                    delay = min(delay * 2, 30)

        return False

    async def _concurrent_retry_sequence(self, event: AstrMessageEvent, request_key: str, remaining_attempts: int) -> bool:
        """并发重试序列：支持指数增长的并发批次"""
        if remaining_attempts <= 0:
            return False
        
        attempts_used = 0
        batch_number = 1
        base_concurrent_count = self.concurrent_retry_count
        
        while attempts_used < remaining_attempts:
            # 计算当前批次的并发数量（指数增长）
            current_concurrent_count = min(
                base_concurrent_count * (2 ** (batch_number - 1)),  # 指数增长: 2, 4, 8, 16...
                remaining_attempts - attempts_used,  # 不超过剩余次数
                max(1, remaining_attempts // 2)  # 避免过度并发，最多使用剩余次数的一半
            )
            
            logger.info(f"启动第 {batch_number} 批次并发重试，同时发起 {current_concurrent_count} 个请求...")
            
            # 执行单次并发批次
            batch_success = await self._single_concurrent_batch(event, request_key, current_concurrent_count)
            if batch_success:
                return True
            
            # 更新计数器
            attempts_used += current_concurrent_count
            batch_number += 1
            
            logger.debug(f"第 {batch_number - 1} 批次并发重试失败，已使用 {attempts_used}/{remaining_attempts} 次")
        
        logger.warning(f"所有 {batch_number - 1} 个并发批次均失败")
        return False
    
    async def _single_concurrent_batch(self, event: AstrMessageEvent, request_key: str, concurrent_count: int) -> bool:
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
                
                if not new_response or not getattr(new_response, 'completion_text', ''):
                    logger.debug(f"并发重试任务 #{attempt_id} 返回空结果")
                    return None
                
                new_text = new_response.completion_text.strip()
                
                # 检查新回复是否包含错误
                new_text_lower = new_text.lower()
                has_error = any(keyword in new_text_lower for keyword in self.error_keywords)
                
                # 状态码检测
                code = self._extract_status_code(new_text)
                if code is not None:
                    if code in self.non_retryable_status_codes:
                        logger.debug(f"并发重试任务 #{attempt_id} 检测到不可重试状态码 {code}")
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
                            logger.debug(f"并发重试任务 #{attempt_id} 获得结果但已有首个结果，丢弃")
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
                    return_when=asyncio.FIRST_COMPLETED
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
            
            # 检查最终结果
            if first_valid_result:
                event.set_result(event.plain_result(first_valid_result))
                logger.info(f"并发批次成功，已取消剩余 {len(remaining_tasks)} 个任务")
                return True
            else:
                logger.debug(f"当前并发批次失败，所有 {concurrent_count} 个任务均未成功")
                return False
                
        except Exception as e:
            logger.error(f"并发批次执行过程中发生异常: {e}")
            # 清理所有任务
            for task in tasks:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            
            # 即使发生异常，也检查是否已有结果
            if first_valid_result:
                event.set_result(event.plain_result(first_valid_result))
                logger.info("异常期间获得有效结果，仍然返回成功")
                return True
            
            return False

    def _handle_retry_failure(self, event: AstrMessageEvent) -> None:
        """处理重试失败的情况（拆分后的失败处理逻辑）"""
        logger.error(f"所有 {self.max_attempts} 次重试均失败")
        
        # 发送兜底回复
        if self.fallback_reply and self.fallback_reply.strip():
            event.set_result(event.plain_result(self.fallback_reply.strip()))
        else:
            event.clear_result()
            event.stop_event()

    @filter.on_decorating_result(priority=-1)
    async def check_and_retry(self, event: AstrMessageEvent):
        """检查结果并进行重试（重构后的主入口方法）"""
        # 钩子执行追踪
        logger.debug(f"[HOOK] on_decorating_result 触发 - 消息: '{event.message_str[:50]}...'")
        logger.debug(f"[HOOK] Event ID: {id(event)}, 类型: {type(event)}")
        
        # 如果禁用重试则直接返回
        if self.max_attempts <= 0:
            logger.debug("[SKIP] 重试功能已禁用")
            return

        # 首先检查是否是内置指令（直接跳过，避免键冲突导致的误判）
        message_text = event.message_str.strip() if event.message_str else ""
        if message_text.startswith('/') and not message_text.startswith('//'):
            # 常见内置指令列表
            builtin_commands = [
                '/help', '/plugin', '/t2i', '/tts', '/sid', '/op', '/deop', '/wl', '/dwl',
                '/provider', '/model', '/ls', '/new', '/groupnew', '/switch', '/rename',
                '/del', '/reset', '/history', '/key', '/persona', '/dashboard_update',
                '/set', '/unset', '/llm', '/alter_cmd', '/tool', '/websearch'
            ]
            
            command = message_text.split()[0].lower()
            if command in builtin_commands:
                logger.debug(f"[SKIP] 检测到内置指令 {command}，跳过重试检查")
                return
        
        # 检查这个消息是否实际经过了LLM处理
        request_key = self._get_request_key(event)
        
        # 检查是否有存储的参数（表明经过了LLM处理）
        has_llm_processing = request_key in self.pending_requests
        
        if not has_llm_processing:
            logger.debug(f"[SKIP] 消息未经LLM处理，跳过重试检查: '{event.message_str}'")
            logger.debug(f"[SKIP] 查找key: {request_key}")
            return

        # 检查是否是LLM实际产生的回复
        llm_response = getattr(event, 'llm_response', None)
        if not llm_response:
            logger.debug(f"[SKIP] 无LLM响应对象，可能是其他插件处理的消息")
            return

        # 检查工具调用
        if hasattr(llm_response, 'choices') and llm_response.choices:
            finish_reason = getattr(llm_response.choices[0], 'finish_reason', None)
            if finish_reason == 'tool_calls':
                logger.debug("检测到正常的工具调用，不进行干预")
                return

        result = event.get_result()
        
        # 检查是否需要重试
        if not self._should_retry_response(result):
            return
        
        # 只有在用户发送了文本内容时才进行重试
        if not event.message_str or not event.message_str.strip():
            logger.debug("用户消息为空，跳过重试")
            return

        logger.info("检测到需要重试的情况，开始重试流程")
        
        # 获取存储的请求参数
        request_key = self._get_request_key(event)
        
        # 执行重试序列
        retry_success = await self._execute_retry_sequence(event, request_key)
        
        # 如果重试失败，处理失败情况
        if not retry_success:
            self._handle_retry_failure(event)
        
        # 清理存储的请求参数（延迟清理）
        self._schedule_cleanup(request_key, delay=60)

    def _schedule_cleanup(self, request_key: str, delay: int = 120):
        """延迟清理参数，给重试流程足够时间"""
        async def delayed_cleanup():
            await asyncio.sleep(delay)
            if request_key in self.pending_requests:
                del self.pending_requests[request_key]
            
        asyncio.create_task(delayed_cleanup())

    async def terminate(self):
        """插件卸载时清理资源"""
        self.pending_requests.clear()
        logger.info("已卸载 [IntelligentRetry] 插件 v2.9.3。")

# --- END OF FILE main.py ---
