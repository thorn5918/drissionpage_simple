# -*- coding:utf-8 -*-
"""
网络请求拦截（对标 Playwright page.route()，基于 CDP Fetch 域实现）

原理（与 Playwright 在 Chromium 上的实现同构）：
1. Fetch.enable 开启拦截后，命中模式的请求在网络栈处理前触发 Fetch.requestPaused 事件
2. 处理器通过 Route 对象决定处理方式：
   - route.continue_()  -> Fetch.continueRequest  放行（可选改写url/method/headers/post_data）
   - route.fulfill(...) -> Fetch.fulfillRequest   用自定义内容应答（本地文件替换/mock接口）
   - route.abort(...)   -> Fetch.failRequest      阻断（屏蔽图片等无关资源加速加载）
3. 未命中任何路由的请求立即放行，行为与未开启拦截时一致

匹配语义与 Playwright 对齐：glob 模式（'*'不含'/'、'**'含'/'、'{a,b}'备选）须匹配整个URL；
也支持正则（search语义）与接收url返回布尔的函数；多个路由命中时后注册的优先。
"""
from base64 import b64encode
from binascii import b2a_base64
from functools import lru_cache
from mimetypes import guess_type
from re import compile as _re_compile, escape as _re_escape, Pattern
from threading import Lock
from traceback import print_exc

from .errors import CDPError, PageDisconnectedError

__all__ = ['RequestInfo', 'Route', 'Interceptor', 'glob_to_regex']

# Fetch域开启参数：urlPattern统一用'*'（CDP的*跨'/'，放行所有请求），
# 具体的Playwright语义匹配由Python侧完成，保证与Playwright行为一致
_FETCH_PATTERN = [{'urlPattern': '*'}]

# abort()可用的失败原因（CDP ErrorReason，Response阶段专用的两项未列出）
_ABORT_REASONS = {'Failed', 'Aborted', 'TimedOut', 'AccessDenied', 'ConnectionClosed',
                  'ConnectionReset', 'ConnectionRefused', 'ConnectionAborted',
                  'ConnectionFailed', 'NameNotResolved', 'InternetDisconnected',
                  'AddressUnreachable', 'BlockedByClient'}


class RequestInfo(object):
    """被拦截请求的只读信息（对应Playwright的route.request）"""

    def __init__(self, request, resource_type):
        self.url = request.get('url', '')
        self.method = request.get('method', '')
        self.headers = dict(request.get('headers') or {})
        self.resource_type = resource_type  # CDP resourceType：Document/Script/Image/XHR/Fetch...
        self.post_data = request.get('postData')  # 文本形式的请求体，可能为None

    def __repr__(self):
        return f'<RequestInfo {self.method} {self.url}>'


class Route(object):
    """一个被暂停请求的处理句柄：continue_()/fulfill()/abort() 三选一，只有首个动作生效"""

    def __init__(self, request_id, request, resource_type, interceptor):
        self._id = request_id
        self.request = RequestInfo(request, resource_type)
        self._interceptor = interceptor
        self._done = False

    def __repr__(self):
        return f'<Route {self._id[:8]} {self.request.method} {self.request.url}>'

    def is_handled(self):
        """该请求是否已被处理（任何动作调用后为True）"""
        return self._done

    def continue_(self, url=None, method=None, headers=None, post_data=None):
        """放行请求，可选改写url/method/headers/post_data（headers为dict）
        :param post_data: str按文本传递；bytes按base64传递
        """
        params = {}
        if url is not None:
            params['url'] = str(url)
        if method is not None:
            params['method'] = str(method)
        if headers:
            params['headers'] = [{'name': str(k), 'value': str(v)} for k, v in headers.items()]
        if post_data is not None:
            params['postData'] = post_data if isinstance(post_data, str) else _to_b64(post_data)
        self._act('Fetch.continueRequest', **params)

    def fulfill(self, status=200, headers=None, content_type=None, body=None, path=None):
        """用自定义内容应答请求，不产生真实网络流量
        :param status: 响应状态码，默认200
        :param headers: 附加响应头dict
        :param content_type: 覆盖Content-Type
        :param body: 响应体，str按UTF-8编码，bytes按二进制
        :param path: 本地文件路径，读取文件内容作为响应体并按扩展名推断Content-Type
        """
        if path is not None:
            with open(path, 'rb') as f:
                data = f.read()
            inferred = guess_type(path)[0]
        elif body is not None:
            data = body.encode('utf-8') if isinstance(body, str) else body
            inferred = None
        else:
            data = b''
            inferred = None

        resp_headers = {str(k): str(v) for k, v in (headers or {}).items()}
        if content_type is not None:
            resp_headers['Content-Type'] = str(content_type)
        elif 'Content-Type' not in resp_headers:
            # Playwright默认text/html；本地文件按扩展名推断
            resp_headers['Content-Type'] = inferred or 'text/html'

        self._act('Fetch.fulfillRequest', responseCode=int(status),
                  responseHeaders=[{'name': k, 'value': v} for k, v in resp_headers.items()],
                  body=_to_b64(data))

    def abort(self, error_reason='Failed'):
        """阻断请求
        :param error_reason: CDP ErrorReason，如 'Failed'、'BlockedByClient'
        """
        if error_reason not in _ABORT_REASONS:
            raise ValueError(f'非法的error_reason：{error_reason}，可用值：{sorted(_ABORT_REASONS)}')
        self._act('Fetch.failRequest', errorReason=error_reason)

    # ---------------- 内部 ----------------
    def _act(self, method, **params):
        """执行处理动作：只有首个动作生效，后续调用忽略"""
        if self._done:
            return
        self._done = True
        self._interceptor._pending.pop(self._id, None)
        try:
            # 连接已断时静默；其余错误（如非法参数）抛给上层打印
            self._interceptor._tab.run_cdp(method, requestId=self._id,
                                           _ignore=PageDisconnectedError, **params)
        except CDPError as e:
            # 请求已被浏览器侧终结（如页面跳转取消）时按无操作对待，避免abort高频场景刷屏
            if 'Invalid InterceptionId' in e.message:
                return
            raise


class Interceptor(object):
    """标签页级拦截器：管理路由表与Fetch域生命周期，每个Tab一个"""

    def __init__(self, tab):
        self._tab = tab
        self._routes = []  # [(pattern, handler)]，按注册顺序，匹配时倒序（后注册优先）
        self._pending = {}  # {requestId: Route}，等待处理动作的请求
        self._enabled = False  # Fetch域是否已启用
        self._lock = Lock()

    # ---------------- 对Tab暴露的API ----------------
    def route(self, pattern, handler):
        """登记路由，首个路由时开启Fetch域"""
        if not callable(handler):
            raise TypeError(f'handler必须是可调用对象，收到：{type(handler).__name__}')
        entry = (pattern, handler)
        with self._lock:
            if entry not in self._routes:  # 相同(pattern, handler)只保留一条
                self._routes.append(entry)
            need_enable = not self._enabled
            self._enabled = True
        if need_enable:
            # 先注册事件回调再enable，避免首个暂停事件漏接
            self._tab.on('Fetch.requestPaused', self._on_request_paused)
            self._tab.run_cdp('Fetch.enable', patterns=_FETCH_PATTERN)

    def unroute(self, pattern=None, handler=None):
        """注销路由；pattern与handler都缺省时注销全部。最后一条移除后关闭Fetch域"""
        with self._lock:
            if pattern is None and handler is None:
                self._routes.clear()
            else:
                self._routes = [(p, h) for p, h in self._routes
                                if not ((pattern is None or _pattern_eq(p, pattern))
                                        and (handler is None or h == handler))]
            empty = not self._routes
        if empty and self._enabled:
            self._stop()

    # ---------------- 事件分发 ----------------
    def _on_request_paused(self, requestId, request, resourceType, **kwargs):
        """Fetch.requestPaused事件入口：匹配路由分发，未匹配立即放行
        任何异常都在此消化（打印堆栈+放行），保证Driver事件线程不被杀死
        """
        try:
            with self._lock:
                routes = self._routes[:]
            handler = next((h for pattern, h in reversed(routes)
                            if _match(pattern, request.get('url', ''))), None)
            if handler is None:
                self._release(requestId)
                return
            route = Route(requestId, request, resourceType, self)
            self._pending[requestId] = route
            handler(route)
        except Exception:
            print_exc()
            try:
                self._release(requestId)
            except Exception:
                pass

    # ---------------- 内部 ----------------
    def _release(self, request_id):
        """放行一个暂停中的请求（尽力而为，连接/目标不存在时静默）"""
        self._pending.pop(request_id, None)
        self._tab.run_cdp('Fetch.continueRequest', requestId=request_id,
                          _ignore=(CDPError, PageDisconnectedError))

    def _stop(self):
        """兜底放行仍在暂停中的请求，然后关闭Fetch域"""
        for request_id in list(self._pending):
            try:
                self._release(request_id)
            except Exception:
                pass
        self._pending.clear()
        try:
            self._tab.run_cdp('Fetch.disable', _ignore=(CDPError, PageDisconnectedError))
        except Exception:
            pass
        self._tab.on('Fetch.requestPaused', None)
        self._enabled = False

    def _reset(self):
        """连接断开时清空全部状态（不发送任何CDP命令），重连后可重新注册"""
        with self._lock:
            self._routes.clear()
            self._pending.clear()
            self._enabled = False

    def __repr__(self):
        return f'<Interceptor {len(self._routes)} routes, {len(self._pending)} pending>'


# ---------------- 模式匹配 ----------------
def _match(pattern, url):
    """判断url是否命中pattern：None匹配所有；正则用search；函数以url调用；glob整URL匹配"""
    if pattern is None:
        return True
    if isinstance(pattern, Pattern):
        return pattern.search(url) is not None
    if callable(pattern):
        return bool(pattern(url))
    return glob_to_regex(pattern).fullmatch(url) is not None


def _pattern_eq(a, b):
    """unroute时的模式相等判断（re.Pattern用对象同一性）"""
    return a is b or a == b


@lru_cache(maxsize=256)
def glob_to_regex(glob):
    """Playwright风格glob转编译后的正则（用于整URL匹配）
    '*'匹配除'/'外任意字符；'**'匹配任意字符；'**/'（后随内容时）为可选的任意路径前缀；
    '?'匹配字面问号；'{a,b}'为备选项；'\\'转义下一字符
    """
    return _re_compile(_parse_glob(glob, 0)[0])


def _parse_glob(g, i):
    """递归解析glob为正则片段，返回(正则字符串, 消耗的字符数)"""
    out = []
    n = len(g)
    while i < n:
        c = g[i]
        if c == '*':
            if g.startswith('**/', i):
                out.append('(?:.*/)?')
                i += 3
            elif g.startswith('**', i):
                out.append('.*')
                i += 2
            else:
                out.append('[^/]*')
                i += 1
        elif c == '?':
            out.append('\\?')
            i += 1
        elif c == '{':
            alts, j = _split_braces(g, i)
            if alts is None:  # 未闭合，按字面处理
                out.append(_re_escape(c))
                i += 1
            else:
                out.append('(?:' + '|'.join(_parse_glob(a, 0)[0] for a in alts) + ')')
                i = j
        elif c == '\\':
            if i + 1 < n:
                out.append(_re_escape(g[i + 1]))
                i += 2
            else:
                out.append(_re_escape(c))
                i += 1
        else:
            out.append(_re_escape(c))
            i += 1
    return ''.join(out), i


def _split_braces(g, i):
    """从g[i]=='{'开始找配对的'}'，按顶层逗号拆分备选项
    :return: (备选列表, '}'后一位下标)；未闭合返回(None, None)
    """
    depth = 1
    j = i + 1
    start = j
    alts = []
    n = len(g)
    while j < n:
        c = g[j]
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                alts.append(g[start:j])
                return alts, j + 1
        elif c == ',' and depth == 1:
            alts.append(g[start:j])
            start = j + 1
        j += 1
    return None, None


def _to_b64(data):
    """bytes转base64字符串（CDP的body/postData格式）"""
    return b2a_base64(data, newline=False).decode('ascii')
