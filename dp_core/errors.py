# -*- coding:utf-8 -*-
"""
错误类型（精简自 DrissionPage/errors.py，去掉多语言层，直接用中文提示）
"""


class BaseError(Exception):
    """所有自定义异常的基类"""

    def __init__(self, message=''):
        super().__init__(message)
        self.message = message

    def __str__(self):
        return self.message


class BrowserConnectError(BaseError):
    """无法连接浏览器调试端口"""
    pass


class CDPError(BaseError):
    """CDP 命令执行出错（浏览器返回 error）"""
    pass


class PageDisconnectedError(BaseError):
    """页面/浏览器连接已断开"""
    pass


class JavaScriptError(BaseError):
    """JS脚本执行出错（脚本抛出异常）"""
    pass
