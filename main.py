#!/usr/bin/env python3
"""
严格测试：模拟真实AstrBot环境的配置读取
"""
import sys
import os
sys.path.insert(0, os.path.abspath('./plugins/astrabot_plugin_retry'))

print("🧪 严格测试：真实环境模拟")
print("=" * 60)

# 测试1: 模拟空配置（你遇到的实际情况）
class EmptyConfigHelper:
    """模拟真实AstrBot返回空配置的情况"""
    def get_plugin_config(self, plugin_name):
        print(f"[空配置测试] 📖 配置助手调用: {plugin_name}")
        print(f"[空配置测试] 📊 返回空字典（真实情况）")
        return {}  # 真实AstrBot返回的空配置

class EmptyContext:
    def __init__(self):
        self.config_helper = EmptyConfigHelper()

# 测试2: 模拟有效配置
class ValidConfigHelper:
    """模拟AstrBot正确返回配置的情况"""
    def get_plugin_config(self, plugin_name):
        print(f"[有效配置测试] 📖 配置助手调用: {plugin_name}")
        if plugin_name == "astrabot_plugin_retry":
            config = {
                "enable_truncation_detection": False,
                "enable_error_keyword_detection": True, 
                "adaptive_delay": False,
                "max_attempts": 8,
                "retry_delay": 3.0,
                "fallback_reply": "用户自定义回复",
                "error_keywords": "用户错误关键词\n网络超时"
            }
            print(f"[有效配置测试] 📊 返回配置: {config}")
            return config
        else:
            print(f"[有效配置测试] ⚠️ 错误插件名: {plugin_name}")
            return {}

class ValidContext:
    def __init__(self):
        self.config_helper = ValidConfigHelper()

# 测试3: 模拟config_helper不存在的情况
class NoConfigHelperContext:
    """模拟AstrBot版本不支持config_helper的情况"""
    def __init__(self):
        # 故意不设置config_helper属性
        pass

# 测试4: 模拟config_helper存在但方法错误的情况
class BrokenConfigHelper:
    """模拟配置助手存在但方法调用失败的情况"""
    def get_plugin_config(self, plugin_name):
        print(f"[错误配置测试] 📖 配置助手调用: {plugin_name}")
        raise Exception("配置系统内部错误")

class BrokenContext:
    def __init__(self):
        self.config_helper = BrokenConfigHelper()

def test_plugin_config(test_name, context_class):
    """测试插件配置读取"""
    print(f"\n🔬 {test_name}")
    print("-" * 40)
    
    try:
        from main import IntelligentRetry
        
        context = context_class()
        plugin = IntelligentRetry(context)
        
        # 检查关键配置
        print(f"结果检查:")
        print(f"  ├─ 截断检测: {plugin.enable_truncation_detection}")
        print(f"  ├─ 错误检测: {plugin.enable_error_keyword_detection}")
        print(f"  ├─ 自适应延迟: {plugin.adaptive_delay}")
        print(f"  ├─ 最大重试: {plugin.max_attempts}")
        print(f"  ├─ 重试延迟: {plugin.retry_delay}")
        print(f"  └─ 兜底回复: {plugin.fallback_reply[:30]}...")
        
        return plugin
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return None

def compare_configs(plugin1, plugin2, test1_name, test2_name):
    """对比两个插件的配置差异"""
    if not plugin1 or not plugin2:
        print("⚠️ 无法对比：插件实例创建失败")
        return
        
    print(f"\n📊 配置对比: {test1_name} vs {test2_name}")
    print("=" * 60)
    
    comparisons = [
        ("截断检测", plugin1.enable_truncation_detection, plugin2.enable_truncation_detection),
        ("错误检测", plugin1.enable_error_keyword_detection, plugin2.enable_error_keyword_detection),
        ("自适应延迟", plugin1.adaptive_delay, plugin2.adaptive_delay),
        ("最大重试", plugin1.max_attempts, plugin2.max_attempts),
        ("重试延迟", plugin1.retry_delay, plugin2.retry_delay),
        ("兜底回复长度", len(plugin1.fallback_reply), len(plugin2.fallback_reply))
    ]
    
    differences = 0
    for name, val1, val2 in comparisons:
        if val1 != val2:
            print(f"  ├─ {name}: {val1} ≠ {val2} {'✅不同' if val1 != val2 else '❌相同'}")
            differences += 1
        else:
            print(f"  ├─ {name}: {val1} = {val2} ❌相同")
    
    print(f"  └─ 总计: {differences}/{len(comparisons)} 项不同")
    
    if differences == 0:
        print("🚨 警告: 所有配置都相同！说明用户配置没有生效！")
    else:
        print("✅ 配置系统工作正常: 不同输入产生不同输出")

# 执行所有测试
print("开始执行严格测试...")

plugin_empty = test_plugin_config("测试1: 空配置（真实情况）", EmptyContext)
plugin_valid = test_plugin_config("测试2: 有效配置（期望情况）", ValidContext)
plugin_no_helper = test_plugin_config("测试3: 无config_helper", NoConfigHelperContext)
plugin_broken = test_plugin_config("测试4: 配置系统错误", BrokenContext)

# 关键对比
compare_configs(plugin_empty, plugin_valid, "空配置", "有效配置")

# 最终评估
print(f"\n🎯 最终评估:")
print("=" * 60)

if plugin_empty and plugin_valid:
    if (plugin_empty.max_attempts == plugin_valid.max_attempts and 
        plugin_empty.retry_delay == plugin_valid.retry_delay and
        plugin_empty.fallback_reply == plugin_valid.fallback_reply):
        print("❌ 测试结论: 配置读取确实有问题！")
        print("   原因: 不同的配置输入产生了相同的结果")
        print("   建议: 检查AstrBot配置系统或插件名称")
    else:
        print("✅ 测试结论: 配置读取逻辑正确!")
        print("   原因: 不同配置输入产生了不同结果")
        print("   建议: 检查AstrBot是否正确保存了用户配置")

print(f"\n🧪 严格测试完成")
