# -*- coding:utf-8 -*-
"""baidu 实测：网络拦截加速效果对比
python test_baidu_demo.py [调试地址]
"""
import sys
from time import perf_counter

from dp_core import Chromium

ADDR = 'http://123.168.144.158:20001/json/list'
BLOCK_PATTERNS = '**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2,mp4}'


def load(tab, url, timeout=30):
    """加载页面，返回(耗时秒, 收到的请求数)"""
    seen = []
    tab.on('Network.requestWillBeSent', lambda requestId, request, **kw: seen.append(request['url']))
    t0 = perf_counter()
    ok = tab.get(url, timeout=timeout)
    cost = perf_counter() - t0
    return ok, cost, len(seen)


def main():
    addr = sys.argv[1] if len(sys.argv) > 1 else ADDR
    browser = Chromium(addr)
    print(f'已连接：{browser.address}  {browser.version}')

    tab = browser.new_tab()
    tab.run_cdp('Network.enable')
    tab.run_cdp('Network.setCacheDisabled', cacheDisabled=True)  # 排除缓存干扰

    try:
        # ---------- 基线：无拦截加载 ----------
        ok, cost, n = load(tab, 'https://www.baidu.com')
        print(f'\n[基线] 加载{"成功" if ok else "失败"}：{cost:.2f}s，网络请求 {n} 个，标题「{tab.title}」')

        # ---------- 拦截：阻断静态资源 ----------
        blocked = []
        tab.route(BLOCK_PATTERNS, lambda r: (blocked.append(r.request.url), r.abort()))
        ok, cost, n = load(tab, 'https://www.baidu.com')
        print(f'\n[拦截] 阻断图片/字体/视频后：{cost:.2f}s，真实网络请求 {n} 个，标题「{tab.title}」')
        print(f'       被阻断 {len(blocked)} 个资源：')
        for u in blocked[:6]:
            print(f'         - {u[:100]}')
        if len(blocked) > 6:
            print(f'         ... 等 {len(blocked)} 个')

        # ---------- 替换：把百度图标css换成本地内容 ----------
        local_css = '/* local override */'
        tab.route('**/cos-icon_*.css', lambda r: r.fulfill(
            status=200, content_type='text/css', body=local_css))
        tab.route(BLOCK_PATTERNS, lambda r: (blocked.append(r.request.url), r.abort()))
        ok, cost, n = load(tab, 'https://www.baidu.com')
        print(f'\n[替换] css被替换为本地内容 + 阻断静态资源：{cost:.2f}s，真实网络请求 {n} 个')
        print(f'       页面仍正常渲染，标题「{tab.title}」')

        # ---------- 验证替换确实生效：直访该css ----------
        tab.route(BLOCK_PATTERNS, lambda r: r.abort())
        tab.get('https://pss.bdstatic.com/r/www/static/font/cosmic/pc/cos-icon_01c45ca.css', timeout=15)
        body = tab.run_js('return document.body.innerText')
        print(f'       直访被替换的css，内容为：「{body.strip()[:30]}」')
        tab.unroute()

        # ---------- 浏览器级路由：新开的裸标签页也被拦截 ----------
        browser_blocked = []
        browser.route(BLOCK_PATTERNS, lambda r: (browser_blocked.append(r.request.url), r.abort()))
        raw_id = browser.run_cdp('Target.createTarget', url='about:blank')['targetId']
        t2 = browser.get_tab(raw_id)
        t2.run_cdp('Network.enable')
        t2.run_cdp('Network.setCacheDisabled', cacheDisabled=True)
        ok, cost, n = load(t2, 'https://www.baidu.com')
        print(f'\n[浏览器级] 裸标签页（未经new_tab创建）加载：{cost:.2f}s，真实网络请求 {n} 个')
        print(f'           浏览器级路由阻断 {len(browser_blocked)} 个资源，标题「{t2.title}」')
        for u in browser_blocked[:3]:
            print(f'             - {u[:100]}')
        browser.unroute()
        browser.close_tab(raw_id)
    finally:
        tab.unroute()
        tab.close()
        browser.disconnect()
    print('\nbaidu 实测完成。')


if __name__ == '__main__':
    main()
