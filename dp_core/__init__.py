# -*- coding:utf-8 -*-
"""
dp_core —— 从 DrissionPage (https://gitee.com/g1879/DrissionPage) 抽取的最小可运行核心

只保留两件事：
1. 通过 remote-debugging-port 接管已有浏览器（绝不查找/启动本地浏览器）
2. 发送 CDP 命令（浏览器级 + 页面级），并支持订阅 CDP 事件

对应关系：
    dp_core/driver.py   <- DrissionPage/_base/driver.py      （WebSocket CDP 驱动）
    dp_core/launcher.py <- DrissionPage/_functions/browser.py（调试端口连接/校验）
    dp_core/browser.py  <- DrissionPage/_base/chromium.py    （Chromium/Tab 对象）
    dp_core/intercept.py <- Playwright page.route 同构实现   （网络请求拦截，基于CDP Fetch域）
    dp_core/tools.py    <- DrissionPage/_functions/tools.py  （端口/等待/错误处理）
    dp_core/errors.py   <- DrissionPage/errors.py            （异常类型）
"""
from .browser import Chromium, Tab, get_browser_id, parse_address
from .driver import Driver, BrowserDriver, CDP_TIMEOUT
from .errors import BrowserConnectError, CDPError, JavaScriptError, PageDisconnectedError
from .intercept import Interceptor, RequestInfo, Route, glob_to_regex

__all__ = ['Chromium', 'Tab', 'parse_address', 'get_browser_id', 'Driver', 'BrowserDriver',
           'BrowserConnectError', 'CDPError', 'JavaScriptError', 'PageDisconnectedError',
           'CDP_TIMEOUT', 'Route', 'RequestInfo', 'Interceptor', 'glob_to_regex']
__version__ = '0.3.0'
