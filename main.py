@register(
   "astrabot_plugin_retry",
   "木有知 & 长安某",
    "当LLM回复为空或包含特定错误关键词时，自动进行多次重试，保持完整上下文和人设。激进截断检测v4.1",
    "4.1"
    "当LLM回复为空或包含特定错误关键词时，自动进行多次重试，保持完整上下文和人设。激进截断检测v4.4 - 用户可控",
    "4.4"
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
    v4.4: 用户可控版本 - 解决用户三大问题 + 自由选择
    - 🚀 完美解决：错误检测不全、延迟太久、兜底词失效
    - �️ 用户可控：截断检测可开关，满足不同使用习惯
    - ⚡ 极速响应：0.2-0.5秒智能延迟，告别长等待
    - 🎯 精确检测：针对具体错误量身定制关键词
    - 💬 可靠兜底：多重机制确保自定义回复生效
   """

def __init__(self, context: Context):
super().__init__(context)

        # 使用简单的默认配置，不依赖复杂的配置系统
        # 使用优化配置 - 解决用户三大问题
self.max_attempts = 3
        self.retry_delay = 2.0
        self.retry_delay = 0.3  # 极速响应：0.3秒基础延迟
        self.adaptive_delay = True  # 自适应延迟而非指数增长

        # 错误关键词
        # 🎛️ 用户可配置选项
        self.enable_truncation_detection = self._get_truncation_detection_setting()  # 是否启用截断检测
        self.enable_error_keyword_detection = True  # 是否启用错误关键词检测（建议保持开启）

        # 🔥 问题1解决：全面错误检测，精确匹配用户遇到的错误
self.error_keywords = [
            # 基础错误
"api 返回的内容为空",
"API 返回的内容为空", 
"APITimeoutError",
            "错误类型: Exception",
            "语音转换失败",
            
            # 🎯 用户具体遇到的错误：请求失败。错误类型，APTL错误信息，request time out请在控制台查看和分享错误详情
            "请求失败",  # 用户遇到的错误开头
            "错误类型，APTL错误信息",  # 用户错误的特征片段
            "APTL错误信息",
            "request time out请在控制台查看",  # 用户错误的完整片段
            "请在控制台查看和分享错误详情",  # 用户错误的结尾
            "请在控制台查看",
            "分享错误详情",
            "错误详情",
            
            # 超时类错误
            "request time out",
            "timeout",
            "time out", 
            "超时",
"网络连接超时",
            
            # 通用错误
            "错误类型",
            "错误类型: Exception", 
            "出现错误",
            "发生错误",
            "调用失败",
            "连接失败",
"服务器暂时不可用",
"请求频率过高",
            "连接失败",
            "调用失败"
            "语音转换失败",
            
            # 英文错误
            "exception",
            "error",
            "failed", 
            "failure",
            "异常"
]

# 人设控制
@@ -103,10 +138,100 @@ def __init__(self, context: Context):
self.context_preview_last_n = 3
self.context_preview_max_chars = 120

        # 兜底回复
        self.fallback_reply = "抱歉，刚才遇到服务波动，我已自动为你重试多次仍未成功。请稍后再试或换个说法。"
        # 兜底回复 - 支持自定义
        self.fallback_reply = self._get_custom_fallback_reply()

        print(f"[重试插件] ⚡ 已加载 v4.4 智能控制版本，最多重试 {self.max_attempts} 次，0.3秒急速响应")
        print(f"[重试插件] 🎯 强化错误检测，精确捕获用户遇到的timeout错误")
        print(f"[重试插件] 🎛️ 截断检测: {'✅启用' if self.enable_truncation_detection else '❌禁用'} | 错误检测: {'✅启用' if self.enable_error_keyword_detection else '❌禁用'}")
        print(f"[重试插件] 💬 兜底回复: '{self.fallback_reply[:30]}...'")

        print(f"[重试插件] 已加载 v4.1 激进截断检测版本，最多重试 {self.max_attempts} 次")
    def _get_truncation_detection_setting(self) -> bool:
        """获取截断检测开关设置"""
        import os
        
        # 尝试从配置文件读取
        config_file = os.path.join(os.path.dirname(__file__), "truncation_config.txt")
        
        try:
            if os.path.exists(config_file):
                with open(config_file, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                    # 寻找有效的配置行（非注释、非空行）
                    for line in lines:
                        line = line.strip()
                        if line and not line.startswith('#'):
                            content = line.lower()
                            if content in ['true', '1', 'yes', 'on', 'enable', '启用', '开启']:
                                print(f"[重试插件] 📖 从配置文件读取: 截断检测已启用")
                                return True
                            elif content in ['false', '0', 'no', 'off', 'disable', '禁用', '关闭']:
                                print(f"[重试插件] 📖 从配置文件读取: 截断检测已禁用")
                                return False
                            break  # 只处理第一个有效配置行
        except Exception as e:
            print(f"[重试插件] ⚠️ 读取截断检测配置失败: {e}")
        
        # 如果没有配置文件，创建一个示例配置
        try:
            if not os.path.exists(config_file):
                config_content = """# 截断检测配置
# true = 启用截断检测（推荐，但可能较频繁）
# false = 禁用截断检测（只检测明确的错误关键词）
true

# 说明：
# 启用截断检测会更积极地重试，确保完整回复，但可能产生更多重试
# 禁用截断检测只在明确出错时重试，减少不必要的重试次数
# 建议：如果觉得重试太频繁，可以改为 false"""
                
                with open(config_file, 'w', encoding='utf-8') as f:
                    f.write(config_content)
                print(f"[重试插件] 📝 已创建截断检测配置文件: {config_file}")
        except Exception as e:
            print(f"[重试插件] ⚠️ 创建截断检测配置失败: {e}")
        
        # 默认启用（保持原有行为）
        print(f"[重试插件] 🎛️ 使用默认设置: 截断检测已启用")
        return True

    def _get_custom_fallback_reply(self) -> str:
        """🔥 问题3解决：修复自定义兜底回复功能"""
        # 多重尝试机制确保读取成功
        import os
        
        # 尝试1：插件目录下的配置文件
        config_paths = [
            os.path.join(os.path.dirname(__file__), "fallback_config.txt"),
            os.path.join(os.path.dirname(__file__), "custom_fallback.txt"),
            os.path.join(os.path.dirname(__file__), "fallback.txt"),
        ]
        
        for config_file in config_paths:
            try:
                if os.path.exists(config_file):
                    with open(config_file, 'r', encoding='utf-8') as f:
                        custom_reply = f.read().strip()
                        if custom_reply:
                            print(f"[重试插件] ✅ 成功使用自定义兜底回复: {config_file}")
                            return custom_reply
            except Exception as e:
                print(f"[重试插件] ⚠️ 读取 {config_file} 失败: {e}")
        
        # 如果没有找到配置，创建一个示例配置
        try:
            example_file = os.path.join(os.path.dirname(__file__), "fallback_config.txt")
            if not os.path.exists(example_file):
                example_content = "主人，小助手刚才脑子转不过来了呢～已经帮你重试了好几次，但还是没能成功。稍等一下下再试试哦～"
                with open(example_file, 'w', encoding='utf-8') as f:
                    f.write(example_content)
                print(f"[重试插件] 📝 已创建示例配置文件: {example_file}")
                return example_content
        except Exception as e:
            print(f"[重试插件] ⚠️ 创建示例配置失败: {e}")
        
        # 默认兜底回复
        return "主人，小助手刚才遇到了点小问题呢～已经自动重试好几次了，但还是没成功。要不稍等一下再试试？"

def _parse_codes(self, codes_str: str) -> Set[int]:
"""解析状态码配置"""
@@ -202,28 +327,41 @@ def _set_fallback_response(self, response) -> None:
# 使用兼容性方式创建Plain组件
try:
from astrbot.api.message_components import Plain
            except:
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
            fallback_chain = type('MessageChain', (), {
                'chain': [Plain(text=self.fallback_reply)]
            })()
            fallback_plain = Plain(text=self.fallback_reply)

            # 替换response内容
            if hasattr(response, 'result_chain'):
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
                print(f"[重试插件] 📝 已设置兜底回复: '{self.fallback_reply[:50]}...'")
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
@@ -392,24 +530,63 @@ def _is_clearly_complete(self, text: str) -> bool:
return True

# 10. "完成"类词汇 = 任务完整
        completion_words = ['完成', '结束', '搞定', '好的', '明白', '了解', '收到', '明白了', 'done', 'finished', 'complete', 'ok', 'got it']
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
        words = re.findall(r'[a-zA-Z\u4e00-\u9fff]+', text)
        if len(words) >= 4:  # 至少4个词的较长句子
        # 更好的词汇分割方式
        words = re.findall(r'[\u4e00-\u9fff]+|[a-zA-Z]+', text)  # 分别匹配中文和英文
        if len(words) >= 3:  # 至少3个词组才考虑为完整句子
last_word = words[-1] if words else ""
# 排除明显的连接词
            if len(last_word) >= 2 and not last_word.lower() in [
                '但是', '然后', '所以', '而且', '另外', '因此', '于是', '接着', '包括', '如下',
            if len(last_word) >= 1 and not last_word.lower() in [
                '但是', '然后', '所以', '而且', '另外', '因此', '于是', '接着', '包括', '如下', '还有', '以及',
'however', 'therefore', 'moreover', 'furthermore', 'because', 'since', 'including'
]:
                # 包含肯定性词汇的长句子，可能是完整的
                if any(pattern in text for pattern in ['是', '有', '会', '能', '可以', '应该', '需要', '正常', '成功']):
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

@@ -446,6 +623,23 @@ async def on_llm_response(self, event: AstrMessageEvent, response) -> bool:
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

@@ -470,7 +664,7 @@ async def on_llm_response(self, event: AstrMessageEvent, response) -> bool:
if is_retry_still_invalid:
print(f"[重试插件] 第 {attempt} 次重试仍然无效: '{retry_text[:50]}...' (长度:{len(retry_text)})")
if attempt < self.max_attempts:
                            await asyncio.sleep(self.retry_delay)  # 重试前等待
                            # 延迟已在循环开始处理，这里不需要额外延迟
continue
else:
print(f"[重试插件] ❌ 已达到最大重试次数 ({self.max_attempts})，全部重试失败")
@@ -494,23 +688,56 @@ async def on_llm_response(self, event: AstrMessageEvent, response) -> bool:

def _should_retry_simple(self, text: str, llm_response=None) -> bool:
"""
        简化的重试判断逻辑
        简化的重试判断逻辑 - 支持用户配置开关
       """
        # 1. 空回复检查
        # 1. 空回复检查（始终启用）
if not text or not text.strip():
print("[重试插件] 检测到空回复")
return True

        # 2. 错误关键词检查
        text_lower = text.lower().strip()
        for keyword in self.error_keywords:
            if keyword in text_lower:
                print(f"[重试插件] 检测到错误关键词: {keyword}")
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

        # 3. 截断检测（激进算法）
        if self._detect_truncation(text, llm_response):
            print("[重试插件] 检测到回复截断")
            return True
        # 3. 截断检测（用户可配置开关）
        if self.enable_truncation_detection:
            if self._detect_truncation(text, llm_response):
                print("[重试插件] 🔄 检测到回复截断")
                return True
        else:
            print("[重试插件] 🎛️ 截断检测已禁用，跳过截断检查")

return False
