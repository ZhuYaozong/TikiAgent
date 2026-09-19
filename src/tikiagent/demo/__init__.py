"""包级兼容导出；内部使用明确模块路径。"""

from importlib import import_module

_EXPORTS = {
    "DemoRunner": ("tikiagent.demo.runner", "DemoRunner"),
    "get_scenario": ("tikiagent.demo.scenarios", "get_scenario"),
}
__all__ = list(_EXPORTS)


def __getattr__(name):
    # 按需加载，避免正式启动带入基线实现。
    if name not in _EXPORTS:
        raise AttributeError(name)
    module, symbol = _EXPORTS[name]
    value = getattr(import_module(module), symbol)
    globals()[name] = value
    return value
