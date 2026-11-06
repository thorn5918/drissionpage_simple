# -*- coding:utf-8 -*-
"""
浏览器连接与 CDP 命令核心（提取自 DrissionPage/_base/chromium.py，Tab 精简自 _pages/chromium_base.py）

只支持"接管已有浏览器"：目标浏览器必须以 --remote-debugging-port=端口 启动。

用法：
    from dp_core import Chromium

    browser = Chromium('123.168.144.158:20001')      # 接管调试端口上的浏览器
    browser = Chromium('http://123.168.144.158:20001/json/list')  # http地址也可以
    r = browser.run_cdp('Browser.getVersion')        # 浏览器级 CDP
    tab = browser.new_tab(url='https://www.baidu.com')
    r = tab.run_cdp('Runtime.evaluate', expression='document.title', returnByValue=True)
"""
from json import dumps, loads, JSONDecodeError
from pathlib import Path
from threading import Lock
from time import sleep

from .driver import BrowserDriver, Driver, CDP_TIMEOUT
from .errors import BrowserConnectError, JavaScriptError, PageDisconnectedError
from .intercept import Interceptor, RouteRegistry
from .launcher import check_connect, parse_address
from .tools import is_js_func, raise_error

__ERROR__ = 'error'

# Runtime.evaluate 无法json序列化的JS字面量（对应原库parse_js_result的unserializableValue分支）
_UNSERIALIZABLE = {'Infinity': float('inf'), '-Infinity': float('-inf'),
                   'NaN': float('nan'), '-0': -0.0}


class Chromium(object):
    """浏览器对象：通过 remote-debugging-port 与浏览器通信，可发送浏览器级 CDP 命令"""
    _BROWSERS = {}
    _lock = Lock()

    def __new__(cls, addr_or_port=None):
        address = parse_address(addr_or_port)
        browser_id, version_info = get_browser_id(address)
        with cls._lock:
            if browser_id in cls._BROWSERS:
                r = cls._BROWSERS[browser_id]
                while not hasattr(r, '_driver'):  # 等待另一个线程完成初始化
                    sleep(.05)
                return r
        r = object.__new__(cls)
        r._address = address
        r.version_info = version_info
        r.id = browser_id
        cls._BROWSERS[browser_id] = r
        return r

    def __init__(self, addr_or_port=None):
        if hasattr(self, '_created'):
            return
        self._created = True

        self._type = 'Chromium'
        self._tabs = {}  # {tab_id: targetInfo}，由目标事件维护
        self._newest_tab_id = None
        self._page_tabs = {}  # {tab_id: Tab}，Tab对象池（get_tab/new_tab/自动挂接复用同一对象）
        self._browser_routes = RouteRegistry()  # 浏览器级拦截路由（browser.route登记处）

        self._driver = BrowserDriver(self.id, 'browser', self.address, self)
        self.version = self.version_info.get('Browser', '')

        # 订阅目标增删事件：标签页开关自动登记/清理
        self._run_cdp('Target.setDiscoverTargets', discover=True)
        self._driver.set_callback('Target.targetCreated', self._onTargetCreated)
        self._driver.set_callback('Target.targetDestroyed', self._onTargetDestroyed)

    # ---------------- 基本信息 ----------------
    @property
    def address(self):
        """调试地址 'ip:port'"""
        return self._address

    @property
    def user_agent(self):
        return self.version_info.get('User-Agent', '')

    # ---------------- CDP ----------------
    def _run_cdp(self, cmd, **cmd_args):
        """浏览器级CDP命令，出错时抛异常"""
        ignore = cmd_args.pop('_ignore', None)
        r = self._driver.run(cmd, **cmd_args)
        return r if __ERROR__ not in r else raise_error(r, ignore)

    def run_cdp(self, cmd, **cmd_args):
        """发送浏览器级 CDP 命令
        :param cmd: 命令名，如 'Browser.getVersion'、'Target.getTargets'
        :param cmd_args: 命令参数；_timeout=x 设置超时秒数（0为只发不等）
        :return: 命令返回的 result 字典
        """
        return self._run_cdp(cmd, **cmd_args)

    def on(self, event, callback):
        """注册浏览器级 CDP 事件回调
        :param event: 事件名，如 'Target.targetCreated'
        :param callback: 回调函数，接收事件params为关键字参数；传None取消
        """
        self._driver.set_callback(event, callback)

    # ---------------- 网络请求拦截（浏览器级，对标 context.route） ----------------
    def route(self, pattern, handler):
        """注册浏览器级拦截路由：对本浏览器的所有页面生效
        与 tab.route 的区别在于作用范围——包括已存在的标签页（含未经 dp_core 创建的）
        和之后新开的标签页（无论由谁创建，通过目标事件自动挂接）。
        同一请求同时命中页面级与浏览器级路由时，页面级优先（对标 Playwright）。
        :param pattern: 匹配模式，同 tab.route（glob/正则/函数）
        :param handler: 处理函数，接收Route对象
        :return: self，可链式调用
        """
        if not callable(handler):
            raise TypeError(f'handler必须是可调用对象，收到：{type(handler).__name__}')
        self._browser_routes.add(pattern, handler)
        # 用HTTP接口的存量标签页清单挂接（与目标事件的到达时序无关，route()后立即生效）
        for info in self.tabs:
            self._attach_interceptor(info['id'])
        return self

    def unroute(self, pattern=None, handler=None):
        """注销浏览器级拦截路由；全部注销后，无本地路由的标签页停止拦截
        :return: self，可链式调用
        """
        empty = self._browser_routes.remove(pattern, handler)
        if empty:
            for tab in list(self._page_tabs.values()):
                if tab._interceptor is not None:
                    tab._interceptor._on_browser_routes_changed()
        return self

    def _attach_interceptor(self, tab_id):
        """为标签页挂上拦截器并确保拦截开启（幂等）；事件线程中调用，异常自行消化。
        直接进Tab对象池，避免get_tab的HTTP校验阻塞事件线程"""
        try:
            tab = self._page_tabs.get(tab_id)
            if tab is None:
                tab = Tab(self, tab_id)
                self._page_tabs[tab_id] = tab
            if tab._interceptor is None:
                tab._interceptor = Interceptor(tab, browser_routes=self._browser_routes)
            tab._interceptor._ensure_enabled()
        except Exception:
            from traceback import print_exc
            print_exc()

    # ---------------- 标签页 ----------------
    @property
    def tabs(self):
        """所有页面型标签页信息列表（来自http接口 /json/list）"""
        j = self._driver.get(f'http://{self.address}/json/list').json()
        return [i for i in j if i['type'] in ('page', 'webview') and not i['url'].startswith('devtools://')]

    @property
    def tab_ids(self):
        return [i['id'] for i in self.tabs]

    @property
    def latest_tab(self):
        """最新的标签页，没有则新建"""
        ids = self.tab_ids
        return self.get_tab(ids[0]) if ids else self.new_tab()

    def get_tab(self, id_or_num=None):
        """获取标签页对象
        :param id_or_num: tab_id字符串，或序号（1为最早打开的页面，-1为最新）
        """
        if id_or_num is None:
            id_or_num = self.tab_ids[0]
        if isinstance(id_or_num, int):
            id_or_num = self.tab_ids[id_or_num]
        elif id_or_num not in self.tab_ids:
            raise RuntimeError(f'未找到标签页：{id_or_num}，当前标签页：{self.tab_ids}')
        tab = self._page_tabs.get(id_or_num)
        if tab is None:
            tab = Tab(self, id_or_num)
            self._page_tabs[id_or_num] = tab
        if len(self._browser_routes):  # 浏览器级路由生效中，确保本页拦截已挂接
            self._attach_interceptor(id_or_num)
        return tab

    def new_tab(self, url=None, new_window=False, background=False):
        """新建标签页并返回Tab对象
        :param url: 新标签页要打开的网址
        :param new_window: 是否在新窗口打开
        :param background: 是否在后台打开
        """
        kwargs = {'url': ''}
        if new_window:
            kwargs['newWindow'] = True
        if background:
            kwargs['background'] = True
        tab_id = self._run_cdp('Target.createTarget', **kwargs)['targetId']
        while self._driver.is_running:  # 等待targetCreated事件登记
            if tab_id in self._tabs:
                break
            sleep(.01)
        else:
            raise BrowserConnectError('浏览器连接已断开。')
        tab = self.get_tab(tab_id)
        if url:
            tab.run_cdp('Page.navigate', url=url)
        return tab

    def close_tab(self, tabs_or_ids):
        """关闭标签页，可传入Tab对象、tab_id或其列表"""
        if isinstance(tabs_or_ids, (Tab, str)):
            tabs_or_ids = [tabs_or_ids]
        for t in tabs_or_ids:
            tab_id = t if isinstance(t, str) else t.tab_id
            self._run_cdp('Target.closeTarget', targetId=tab_id, _ignore=PageDisconnectedError)
            while tab_id in self._tabs and self._driver.is_running:
                sleep(.01)

    # ---------------- 事件维护 ----------------
    def _onTargetCreated(self, **kwargs):
        if (kwargs['targetInfo']['type'] in ('page', 'webview')
                and not kwargs['targetInfo']['url'].startswith('devtools://')):
            tab_id = kwargs['targetInfo']['targetId']
            self._tabs[tab_id] = kwargs['targetInfo']
            self._newest_tab_id = tab_id
            if len(self._browser_routes):  # 浏览器级路由生效中，自动挂接新标签页
                self._attach_interceptor(tab_id)

    def _onTargetDestroyed(self, **kwargs):
        tab_id = kwargs['targetId']
        self._tabs.pop(tab_id, None)
        tab = self._page_tabs.pop(tab_id, None)
        if tab is not None and tab._driver is not None and tab._driver.is_running:
            tab._driver.stop()  # 释放自动挂接的页面连接与线程

    def _on_disconnect(self):
        """websocket断开时由Driver调用"""
        Chromium._BROWSERS.pop(self.id, None)

    def reconnect(self):
        """重新连接浏览器"""
        self._driver.stop()
        BrowserDriver.BROWSERS.pop(self.id, None)
        self.id, self.version_info = get_browser_id(self._address)
        self._driver = BrowserDriver(self.id, 'browser', self.address, self)
        self._tabs = {}
        for tab in list(self._page_tabs.values()):  # 释放旧标签页连接，重连后自动重建
            if tab._driver is not None and tab._driver.is_running:
                tab._driver.stop()
        self._page_tabs.clear()
        self._run_cdp('Target.setDiscoverTargets', discover=True)
        self._driver.set_callback('Target.targetCreated', self._onTargetCreated)
        self._driver.set_callback('Target.targetDestroyed', self._onTargetDestroyed)

    # ---------------- 断开/退出 ----------------
    def disconnect(self):
        """只断开本程序的连接，浏览器保持运行"""
        for tab in list(self._page_tabs.values()):  # 释放自动挂接/使用过的页面连接与线程
            if tab._driver is not None and tab._driver.is_running:
                tab._driver.stop()
        self._page_tabs.clear()
        self._browser_routes.clear()
        self._driver.stop()
        Chromium._BROWSERS.pop(self.id, None)

    def quit(self):
        """关闭浏览器（会真的关掉远端浏览器进程，慎用）"""
        try:
            self._run_cdp('Browser.close', _ignore=PageDisconnectedError)
        except PageDisconnectedError:
            pass
        self.disconnect()

    def __repr__(self):
        return f'<Chromium {self.address}>'


class Tab(object):
    """标签页对象：持有独立的页面级 WebSocket 驱动，可发送页面级 CDP 命令"""

    def __init__(self, browser: Chromium, tab_id: str):
        self.browser = browser
        self.tab_id = tab_id
        self._driver = None
        self._driver_lock = Lock()  # 保护懒加载，避免并发创建两条页面连接
        self._interceptor = None  # 网络请求拦截器，route()时懒创建
        self._created = True

    @property
    def driver(self) -> Driver:
        """页面级驱动（懒建立，并发安全）"""
        with self._driver_lock:
            if self._driver is None or not self._driver.is_running:
                self._driver = Driver(self.tab_id, 'page', self.browser.address, owner=self)
            return self._driver

    # ---------------- CDP ----------------
    def run_cdp(self, cmd, **cmd_args):
        """发送页面级 CDP 命令
        :param cmd: 命令名，如 'Page.navigate'、'Runtime.evaluate'、'Network.enable'
        :param cmd_args: 命令参数；_timeout=x 设置超时秒数
        :return: 命令返回的 result 字典
        """
        ignore = cmd_args.pop('_ignore', None)
        r = self.driver.run(cmd, **cmd_args)
        return r if __ERROR__ not in r else raise_error(r, ignore)

    def on(self, event, callback):
        """注册页面级 CDP 事件回调，如 tab.on('Network.responseReceived', cb)"""
        self.driver.set_callback(event, callback)

    # ---------------- 网络请求拦截 ----------------
    def route(self, pattern, handler):
        """注册网络请求拦截路由（对标 Playwright page.route）
        :param pattern: 匹配模式，四种形式：
            1. glob字符串：须匹配整个URL。'*'匹配除'/'外任意字符，'**'匹配任意字符，
               '{a,b}'备选，如 '**/*.{png,jpg}'、'**/api/**'；'**'匹配所有请求
            2. 编译后的正则：re.search语义
            3. 函数：接收url字符串，返回bool
        :param handler: 处理函数，接收Route对象，通过 route.continue_() / route.fulfill(...) /
            route.abort() 处理请求；handler内抛出的异常会被打印，请求被放行
        :return: self，可链式调用
        示例：
            tab.route('**/*.{png,jpg,woff2}', lambda r: r.abort())          # 阻断图片字体
            tab.route('**/app.js', lambda r: r.fulfill(path='./app.js'))    # 本地文件替换
            tab.route('**/api/user', lambda r: r.fulfill(body='{"id":1}',
                                                content_type='application/json'))  # mock接口
        """
        if self._interceptor is None:
            self._interceptor = Interceptor(self, browser_routes=self.browser._browser_routes)
        self._interceptor.route(pattern, handler)
        return self

    def unroute(self, pattern=None, handler=None):
        """注销拦截路由
        :param pattern: 只注销匹配该模式的路由；None为不限
        :param handler: 只注销该处理函数；None为不限
        两者都缺省时注销全部路由，并停止拦截
        :return: self，可链式调用
        """
        if self._interceptor is not None:
            self._interceptor.unroute(pattern, handler)
        return self

    # ---------------- JS执行 ----------------
    def run_js(self, script, *args, as_expr=False, timeout=None):
        """执行JavaScript代码并返回结果（对应原库run_js）
        :param script: js代码，三种形式：
            1. 函数体：用 return 返回值，如 'return document.title'
            2. 完整函数声明：如 'function(a, b){return a + b}'，配合args调用
            3. js文件路径：自动读取文件内容
        :param args: 传给脚本的参数（str/int/float/bool/None/dict/list），
                     函数体形式里用 arguments[0]、arguments[1]... 访问
        :param as_expr: True时把script作为表达式执行，如 'document.title'
        :param timeout: 超时秒数
        :return: 脚本返回值；Promise会被等待；对象/数组自动转成Python的dict/list
        """
        return self._run_js(script, *args, as_expr=as_expr, timeout=timeout)

    def run_async_js(self, script, *args, as_expr=False):
        """以异步方式执行js，不等待返回（对应原库run_async_js）"""
        return self._run_js(script, *args, as_expr=as_expr, timeout=0)

    def _run_js(self, script, *args, as_expr=False, timeout=None):
        # 与原库一致：script可以是js文件路径
        try:
            if Path(script).exists():
                with open(script, 'r', encoding='utf-8') as f:
                    script = f.read()
        except (OSError, ValueError):
            pass

        expr = script if as_expr else _wrap_js_func(script, args)
        res = self.run_cdp('Runtime.evaluate', expression=expr, returnByValue=True,
                           awaitPromise=True, userGesture=True,
                           _timeout=CDP_TIMEOUT if timeout is None else timeout)

        exc = res.get('exceptionDetails')
        if exc:
            desc = (exc.get('exception', {}) or {}).get('description') or exc.get('text', '')
            raise JavaScriptError(f'JS执行出错：{desc}\n脚本：{script[:200]}')

        result = res.get('result', {})
        if 'unserializableValue' in result:  # Infinity/NaN等无法json序列化的值
            v = result['unserializableValue']
            return _UNSERIALIZABLE.get(v, v)
        if 'objectId' in result:  # 过大或循环引用的对象，退回JSON.stringify
            r = self.run_cdp('Runtime.callFunctionOn',
                             functionDeclaration='function(){return JSON.stringify(this);}',
                             objectId=result['objectId'], returnByValue=True, userGesture=True)
            return loads(r['result']['value'])
        return result.get('value')

    # ---------------- JSON接口 ----------------
    def json(self, url=None, timeout=30):
        """获取接口返回的JSON并解析为Python对象（适用于直接访问接口url的场景）
        :param url: 传入则先导航到该接口地址并等待加载完成；None则解析当前页面内容
        :return: dict或list
        """
        if url is not None:
            self.get(url, timeout=timeout)
        text = self.run_js('return document.body ? document.body.innerText : "";', timeout=timeout)
        try:
            return loads(text)
        except (JSONDecodeError, TypeError):
            raise ValueError(f'页面内容不是合法JSON，开头内容：\n{(text or "")[:200]}')

    # ---------------- 常用信息（CDP实现） ----------------
    @property
    def url(self):
        """当前网址（通过 Target.getTargetInfo 获取）"""
        return self.browser._run_cdp('Target.getTargetInfo', targetId=self.tab_id)['targetInfo']['url']

    @property
    def title(self):
        """当前标题"""
        r = self.run_cdp('Runtime.evaluate', expression='document.title', returnByValue=True)
        return r['result']['value']

    def get(self, url, timeout=30):
        """导航到指定网址并等待加载完成
        :param url: 网址
        :param timeout: 加载超时秒数
        :return: 是否成功
        """
        self.run_cdp('Page.enable')
        done = {'flag': False}

        def on_load(**kwargs):
            done['flag'] = True

        self.on('Page.loadEventFired', on_load)
        try:
            self.run_cdp('Page.navigate', url=url)
            from time import perf_counter
            end_time = perf_counter() + timeout
            while perf_counter() < end_time:
                if done['flag']:
                    return True
                sleep(.1)
            return False
        finally:
            self.on('Page.loadEventFired', None)

    def close(self):
        """关闭此标签页"""
        self.browser.close_tab(self)

    def _on_disconnect(self):
        """页面websocket断开（页面被关闭/跳转）时由Driver调用"""
        self._driver = None
        if self._interceptor is not None:
            self._interceptor._reset()

    def __repr__(self):
        return f'<Tab {self.tab_id}>'


# ---------------- 工具函数 ----------------
def _wrap_js_func(script, args):
    """把脚本包装成可evaluate的表达式（等价于原库Runtime.callFunctionOn的效果）
    - 函数体：包成 function 并立即调用，arguments[i] 可访问
    - 函数声明：直接以 (script)(args) 形式调用
    """
    if is_js_func(script):
        if args:
            return f'({script})({", ".join(dumps(a) for a in args)})'
        return f'({script})()'
    if args:
        return f'(function(){{{script}}}).apply(null, {dumps(list(args))})'
    return f'(function(){{{script}}})()'


def get_browser_id(address):
    """获取浏览器的browser_id与版本信息
    :param address: 'ip:port'
    :return: (browser_id, /json/version信息dict)
    """
    version_info = check_connect(address)
    if 'webSocketDebuggerUrl' not in version_info:
        raise BrowserConnectError(f'{address} 上的程序不是可控制的浏览器调试端口。')
    # 注意：webSocketDebuggerUrl 里的 host 可能与实际访问地址不一致（如经过代理/端口映射），
    # 因此只取最后一段作为browser_id，websocket连接地址由 Driver 按 address 自行拼接（与原库一致）
    browser_id = version_info['webSocketDebuggerUrl'].split('/')[-1]
    return browser_id, version_info
