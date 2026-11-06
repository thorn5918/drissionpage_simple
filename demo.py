# -*- coding:utf-8 -*-
"""
dp_core 演示：连接调试端口 + 发送 CDP 命令 + 订阅 CDP 事件 + 网络请求拦截

用法（地址支持 'ip:port'、'http://ip:port/json/list'、int端口）：
    python demo.py http://123.168.144.158:20001/json/list
    python demo.py 123.168.144.158:20001
    python demo.py            # 默认连 127.0.0.1:9222

注意：演示只操作自己新建的标签页，结束只断开连接，不动浏览器本身。
"""
import sys
from time import sleep

from dp_core import Chromium

ADDR = '127.0.0.1:9222'


def main():
    addr = sys.argv[1] if len(sys.argv) > 1 else ADDR
    browser = Chromium(addr)
    print(f'已连接：{browser.address}')
    print(f'浏览器版本：{browser.version}')

    # ---------- 1. 浏览器级 CDP ----------
    r = browser.run_cdp('Browser.getVersion')
    print('Browser.getVersion ->', {k: r[k] for k in ('product', 'userAgent')})
    print(f'当前标签页数：{len(browser.tab_ids)}')

    # ---------- 2. 页面级 CDP：新建标签页并导航 ----------
    tab = browser.new_tab()
    tab.run_cdp('Page.enable')

    # ---------- 3. 订阅页面级 CDP 事件：捕获网络请求 ----------
    requests_seen = []

    def on_request(requestId, request, **kwargs):
        requests_seen.append(request['url'])

    tab.on('Network.requestWillBeSent', on_request)
    tab.run_cdp('Network.enable')

    tab.get('https://www.baidu.com')
    print(f'页面标题：{tab.title}')
    print(f'页面URL：{tab.url}')
    print(f'捕获到 {len(requests_seen)} 个网络请求，前3个：{requests_seen[:3]}')

    # ---------- 4. 执行JS（有返回值） ----------
    print('readyState ->', tab.run_js('return document.readyState'))
    print('1+2 ->', tab.run_js('function(a, b){return a + b}', 1, 2))
    print('UA(表达式) ->', tab.run_js('navigator.userAgent', as_expr=True))

    # ---------- 5. 网络请求拦截：阻断静态资源 + mock接口 ----------
    # 注：命中浏览器本地缓存的资源不经过网络栈，拦截不到；禁用缓存后演示更直观
    tab.run_cdp('Network.enable')
    tab.run_cdp('Network.setCacheDisabled', cacheDisabled=True)
    blocked = []
    tab.route('**/*.{png,jpg,jpeg,gif,webp,ico,woff,woff2}',
              lambda r: (blocked.append(r.request.url), r.abort()))
    tab.route('**/dp_demo_mock',
              lambda r: r.fulfill(content_type='application/json', body='{"from": "local-mock"}'))
    tab.get('https://www.baidu.com')
    print(f'拦截：阻断了 {len(blocked)} 个静态资源请求')
    tab.get('https://dp-mock.invalid/dp_demo_mock')
    print('mock接口应答 ->', tab.json())
    tab.unroute()

    # ---------- 6. 直接访问接口url并拿JSON ----------
    data = tab.json('https://push2his.eastmoney.com/api/qt/stock/kline/get?cb=&secid=0.000001'
                    '&ut=fa5fd1943c7b386f172d6893dbfba10b&fields1=f1,f2,f3&fields2=f51,f52,f53'
                    '&klt=101&fqt=1&end=20500101&lmt=3')
    print('接口JSON ->', {k: data[k] for k in ('rc', 'rt') if k in data}, 'klines:', data.get('data', {}).get('klines'))

    # ---------- 7. 清理：只关自己开的标签页，断开连接不关浏览器 ----------
    tab.close()
    browser.disconnect()
    print('演示完成。')


if __name__ == '__main__':
    main()
