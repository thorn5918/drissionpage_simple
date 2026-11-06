# -*- coding:utf-8 -*-
"""网络拦截自测脚本：python test_intercept.py [调试地址]

默认对 http://123.168.144.158:20001/json/list 测试。
mock场景使用 .invalid 保留域名（拦截发生在DNS之前，fulfill可直接应答），
只操作自建的标签页；结束断开连接，不关闭浏览器。
"""
import re
import sys
from os import close, remove
from tempfile import mkstemp

from dp_core import Chromium, glob_to_regex
from dp_core.intercept import _match

ADDR = 'http://123.168.144.158:20001/json/list'

LOCAL_HTML = ('<html><head><title>dp_local_ok</title></head>'
              '<body><p>dp local content</p></body></html>')
MOCK_JSON = '{"a": 1, "list": [1, 2, 3]}'


# ---------------- 单元测试（无需浏览器） ----------------
def test_glob_matching():
    """glob转正则：逐条对照Playwright文档示例"""
    m = lambda p, u: glob_to_regex(p).fullmatch(u) is not None
    # '**/*.js' 同时匹配带路径与不带路径的js
    assert m('**/*.js', 'https://example.com/file.js')
    assert m('**/*.js', 'https://example.com/path/file.js')
    # '*' 不跨'/'
    assert m('https://example.com/*.js', 'https://example.com/file.js')
    assert not m('https://example.com/*.js', 'https://example.com/path/file.js')
    # '{a,b}' 备选
    assert m('**/*.{png,jpg}', 'https://x.com/a.png')
    assert m('**/*.{png,jpg}', 'https://x.com/deep/dir/b.jpg')
    assert not m('**/*.{png,jpg}', 'https://x.com/a.gif')
    # '?' 只匹配字面问号
    assert m('https://example.com/?page=1', 'https://example.com/?page=1')
    assert not m('https://example.com/?page=1', 'https://example.com/Xpage=1')
    # 必须匹配整个URL
    assert not m('**/api', 'https://x.com/api/extra')
    assert m('**/api', 'https://x.com/api')
    # '**' 匹配所有请求
    assert m('**', 'https://whatever.com/any/thing?q=1')
    print('[ok] glob匹配：对照Playwright文档示例全部通过')


def test_other_matchers():
    """正则/函数/None三种匹配模式"""
    assert _match(re.compile(r'\.jpeg$'), 'https://x.com/a.jpeg')
    assert not _match(re.compile(r'\.jpeg$'), 'https://x.com/a.png')
    assert _match(lambda u: 'token' in u, 'https://x.com/?token=1')
    assert not _match(lambda u: 'token' in u, 'https://x.com/')
    assert _match(None, 'https://x.com/')
    print('[ok] 正则(search语义)/函数/None匹配')


# ---------------- 集成测试（需要浏览器） ----------------
def _make_local_html():
    """生成临时html文件，返回(路径, 清理函数)"""
    fd, path = mkstemp(suffix='.html')
    with open(fd, 'w', encoding='utf-8') as f:
        f.write(LOCAL_HTML)
    return path, lambda: remove(path)


def test_fulfill_local_file(tab):
    """本地文件替换远程内容：拦截发生在DNS之前，.invalid域名也能被fulfill"""
    path, cleanup = _make_local_html()
    try:
        tab.route('**/dp_local_test.html', lambda r: r.fulfill(path=path))
        assert tab.get('https://dp-mock.invalid/dp_local_test.html', timeout=15), '页面应加载本地内容'
        assert tab.title == 'dp_local_ok', tab.title
        assert 'dp local content' in tab.run_js('return document.body.innerText')
    finally:
        tab.unroute()
        cleanup()
    print('[ok] fulfill(path=本地文件)：远程内容被本地文件替换')


def test_fulfill_mock_json(tab):
    """mock接口：自定义JSON应答"""
    tab.route('**/dp_mock_api', lambda r: r.fulfill(
        status=200, content_type='application/json', body=MOCK_JSON))
    try:
        assert tab.get('https://dp-mock.invalid/dp_mock_api', timeout=15)
        assert tab.json() == {'a': 1, 'list': [1, 2, 3]}
    finally:
        tab.unroute()
    print('[ok] fulfill(body=...)：接口被mock且tab.json()可正常解析')


def test_request_info(tab):
    """Route对象携带的请求信息"""
    path, cleanup = _make_local_html()
    infos = []

    def handler(r):
        infos.append((r.request.url, r.request.method, r.request.resource_type, r.is_handled()))
        r.fulfill(path=path)

    try:
        tab.route('**/dp_info_test.html', handler)
        assert tab.get('https://dp-mock.invalid/dp_info_test.html', timeout=15)
        assert len(infos) == 1, infos
        url, method, rtype, handled = infos[0]
        assert url == 'https://dp-mock.invalid/dp_info_test.html', url
        assert method == 'GET' and rtype == 'Document' and not handled
    finally:
        tab.unroute()
        cleanup()
    print(f'[ok] Route.request信息：{infos[0][:3]}，处理前is_handled()=False')


def test_route_priority(tab):
    """多条路由命中时，后注册的优先"""
    path, cleanup = _make_local_html()
    broad_calls = []
    try:
        tab.route('**/*prio.html', lambda r: (broad_calls.append(r), r.abort()))
        tab.route('**/*.html', lambda r: r.fulfill(path=path))  # 后注册，应胜出
        assert tab.get('https://dp-mock.invalid/dp_prio.html', timeout=15), '应加载本地内容'
        assert tab.title == 'dp_local_ok'
        assert not broad_calls, '先注册的路由不应被调用'
    finally:
        tab.unroute()
        cleanup()
    print('[ok] 路由优先级：后注册的处理器胜出，先注册的未被调用')


def test_continue_idempotent(tab):
    """首个动作生效：continue_()之后abort()被忽略，页面正常加载"""
    def double(r):
        r.continue_()
        r.abort()

    tab.route('**', double)
    try:
        assert tab.get('https://www.baidu.com', timeout=25), 'continue_生效，页面应正常加载'
    finally:
        tab.unroute()
    print('[ok] 动作幂等：continue_()后的abort()被忽略')


def test_continue_modify_headers(tab):
    """改写请求头后放行，服务端可见自定义头"""
    def add_token(r):
        r.continue_(headers={**r.request.headers, 'X-Dp-Token': 'dp123'})

    tab.route('**/postman-echo.com/**', add_token)
    try:
        assert tab.get('https://postman-echo.com/get?dp=intercept', timeout=25), '请求应正常放行'
        headers = tab.json().get('headers', {})
        assert headers.get('x-dp-token') == 'dp123', headers
    finally:
        tab.unroute()
    print(f'[ok] continue_(headers=...)：服务端收到自定义头 x-dp-token')


def test_handler_exception(tab):
    """handler抛异常：请求被兜底放行，事件线程存活，后续拦截正常"""
    def boom(r):
        raise RuntimeError('boom')

    tab.route('**/dp_boom.html', boom)
    try:
        # 请求被兜底放行到真实网络：DNS失败后Chrome加载错误页（title为域名）
        assert tab.get('https://dp-mock.invalid/dp_boom.html', timeout=8)
        assert tab.title == 'dp-mock.invalid', f'应显示DNS错误页，实际title：{tab.title}'
        # 事件线程仍存活：新的fulfill路由正常工作
        tab.route('**/dp_alive.html', lambda r: r.fulfill(body='alive-ok'))
        assert tab.get('https://dp-mock.invalid/dp_alive.html', timeout=15)
        assert tab.run_js('return document.body.innerText') == 'alive-ok'
    finally:
        tab.unroute()
    print('[ok] handler异常：请求被兜底放行且未杀死事件线程，后续拦截正常')


def test_abort_and_unroute(tab):
    """阻断资源与unroute恢复"""
    tab.route('**/*.{png,jpg,gif,webp,svg,ico,css,woff,woff2}', lambda r: r.abort())
    try:
        assert tab.get('https://www.baidu.com', timeout=25), '阻断静态资源后页面仍应加载'
    finally:
        tab.unroute()

    tab.route('**', lambda r: r.abort())
    try:
        assert tab.get('https://www.baidu.com', timeout=15)
        assert tab.title != '百度一下，你就知道', '全部阻断时页面不应真正加载'
    finally:
        tab.unroute()
    assert tab.get('https://www.baidu.com', timeout=25)
    assert tab.title == '百度一下，你就知道', 'unroute后应恢复正常页面'
    print('[ok] abort/unroute：阻断资源→全部阻断→unroute恢复，三阶段行为正确')


def test_full_flow(addr):
    browser = Chromium(addr)
    tab = browser.new_tab()
    try:
        test_fulfill_local_file(tab)
        test_fulfill_mock_json(tab)
        test_request_info(tab)
        test_route_priority(tab)
        test_continue_idempotent(tab)
        test_continue_modify_headers(tab)
        test_handler_exception(tab)
        test_abort_and_unroute(tab)
    finally:
        tab.close()
    print('\n全部通过。')


if __name__ == '__main__':
    addr = sys.argv[1] if len(sys.argv) > 1 else ADDR
    test_glob_matching()
    test_other_matchers()
    test_full_flow(addr)
