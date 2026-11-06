# -*- coding:utf-8 -*-
"""dp_core 自测脚本：python test_dp_core.py [调试地址]

默认对 http://123.168.144.158:20001/json/list 测试。
只操作自建的标签页；结束断开连接，不关闭浏览器。
"""
import sys
from time import sleep

from dp_core import (Chromium, BrowserConnectError, CDPError, JavaScriptError, PageDisconnectedError,
                     parse_address)

ADDR = 'http://123.168.144.158:20001/json/list'


def test_parse_address():
    assert parse_address(None) == '127.0.0.1:9222'
    assert parse_address('http://123.168.144.158:20001/json/list') == '123.168.144.158:20001'
    assert parse_address('123.168.144.158:20001') == '123.168.144.158:20001'
    assert parse_address('http://localhost:9222') == '127.0.0.1:9222'
    assert parse_address(9222) == '127.0.0.1:9222'
    print('[ok] parse_address')


def test_connect_error():
    """连一个不存在的调试端口，应抛 BrowserConnectError"""
    try:
        Chromium('127.0.0.1:1')
    except BrowserConnectError as e:
        print(f'[ok] 连接失败抛 BrowserConnectError：{e}')
        return
    raise AssertionError('应当抛出 BrowserConnectError')


def test_run_js(tab):
    """JS执行：函数体、参数、函数声明、Promise、表达式、报错"""
    assert tab.run_js('return 1 + 1') == 2
    assert tab.run_js('return arguments[0] + arguments[1]', 3, 4) == 7
    assert tab.run_js('function(a, b){return a * b}', 5, 6) == 30
    assert tab.run_js('return new Promise(r => setTimeout(() => r("ok"), 200))') == 'ok'
    assert tab.run_js('({a: 1, b: [1, 2]})', as_expr=True) == {'a': 1, 'b': [1, 2]}
    assert tab.run_js('return [1, 2, 3].map(x => x * 2)') == [2, 4, 6]
    assert tab.run_js('return null') is None
    assert tab.run_js('return {name: "abc", tags: ["x", "y"]}') == {'name': 'abc', 'tags': ['x', 'y']}
    try:
        tab.run_js('return notExist.x')
    except JavaScriptError:
        print('[ok] run_js：函数体/参数/声明/Promise/表达式/对象返回/报错抛JavaScriptError')
        return
    raise AssertionError('应当抛出 JavaScriptError')


def test_json(tab):
    """tab.json()：解析当前页面内容、直接访问接口url"""
    # 解析当前页面：往空白页注入JSON文本
    tab.get('about:blank')
    tab.run_js('document.body.innerText = \'{"a": 1, "list": [1, 2, 3]}\'')
    assert tab.json() == {'a': 1, 'list': [1, 2, 3]}

    # 直接访问接口url并解析（走浏览器侧网络）
    api = ('https://push2his.eastmoney.com/api/qt/stock/kline/get?cb=&secid=0.000001'
           '&ut=fa5fd1943c7b386f172d6893dbfba10b&fields1=f1,f2,f3,f4,f5,f6'
           '&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61&klt=101&fqt=1&end=20500101&lmt=5')
    r = tab.json(api)
    assert isinstance(r, dict) and 'data' in r, str(r)[:200]
    print('[ok] json()：解析当前页面内容 + 访问接口url并解析')


def test_full_flow(addr):
    browser = Chromium(addr)
    assert browser.version, '应能取到浏览器版本'
    print(f'[ok] 连接浏览器：{browser.version} @ {browser.address}')
    print(f'     User-Agent: {browser.user_agent}')

    # 浏览器级 CDP
    r = browser.run_cdp('Browser.getVersion')
    assert 'userAgent' in r  # CDP返回camelCase键，与http接口的User-Agent不同
    print(f'[ok] 浏览器级 CDP：Browser.getVersion -> {r["product"]}')

    # 浏览器级 CDP 事件：目标创建
    created = []
    browser.on('Target.targetCreated', lambda targetInfo, **kw: created.append(targetInfo['targetId']))

    # 现有标签页清单
    ids = browser.tab_ids
    print(f'[ok] 标签页清单：{len(ids)} 个')

    # 新建标签页 + 页面级 CDP
    tab = browser.new_tab()
    assert tab.tab_id in browser.tab_ids
    print(f'[ok] 新建标签页：{tab.tab_id}')
    assert tab.tab_id in created, 'targetCreated事件应被捕获'
    print('[ok] 浏览器级事件回调：Target.targetCreated')

    tab.run_cdp('Page.enable')
    r = tab.run_cdp('Page.navigate', url='https://www.baidu.com')
    assert 'errorText' not in r, r
    assert tab.get('https://www.baidu.com'), '页面应加载成功'
    print(f'[ok] 页面级 CDP：Page.navigate -> {tab.url}')

    # Runtime.evaluate
    r = tab.run_cdp('Runtime.evaluate', expression='document.title', returnByValue=True)
    assert '百度' in r['result']['value'], r
    print(f"[ok] Runtime.evaluate -> {r['result']['value']}")

    # CDP 事件订阅
    seen = []
    tab.on('Network.requestWillBeSent', lambda requestId, request, **kw: seen.append(request['url']))
    tab.run_cdp('Network.enable')
    tab.run_cdp('Page.navigate', url='https://www.baidu.com/')
    sleep(3)
    assert any('baidu.com' in u for u in seen), seen
    print(f'[ok] 页面级事件回调：捕获 {len(seen)} 个网络请求')

    # JS执行
    test_run_js(tab)

    # json接口
    test_json(tab)

    # CDP 命令报错应抛 CDPError
    try:
        tab.run_cdp('Page.navigate', url='::bad url::')
    except (CDPError, PageDisconnectedError) as e:
        print(f'[ok] CDP 出错抛异常：{type(e).__name__}')

    # 同一浏览器复用同一 Chromium 对象（原库单例行为）
    b2 = Chromium(addr)
    assert b2 is browser
    print('[ok] 同一浏览器复用同一 Chromium 对象')

    # 清理：只关自己开的标签页，断开连接不关浏览器
    tab.close()
    assert tab.tab_id not in browser.tab_ids
    print('[ok] 关闭测试标签页')

    browser.disconnect()
    print('\n全部通过。')


if __name__ == '__main__':
    addr = sys.argv[1] if len(sys.argv) > 1 else ADDR
    test_parse_address()
    test_connect_error()
    test_full_flow(addr)
