# -*- coding:utf-8 -*-
"""批量采集 collect 自测脚本：python test_collect.py [调试地址]

单元测试无需浏览器：F构建、两种声明形态等价、处理器管道、参数校验。
集成测试默认对 http://123.168.144.158:20001/json/list 测试，
自建标签页注入固定HTML列表后采集对照预期；结束断开连接，不关闭浏览器。
"""
import sys

from dp_core import Chromium, F
from dp_core.collect import _apply_steps, collect as _collect_raw

ADDR = 'http://123.168.144.158:20001/json/list'

LIST_HTML = '''<ul id="list">
<li class="it"><a href="/p/1" data-id="1">  标题一
  </a><span class="tag">甲</span><span class="tag">乙</span></li>
<li class="it"><a href="/p/2" data-id="2">标题  二</a><span class="tag">丙</span></li>
<li class="it"><a href="/p/3">标题三</a></li>
</ul><div id="total">共 23 条</div>'''


# ---------------- 单元测试（无需浏览器） ----------------
def test_f_build():
    """F链式构建与dict形态等价；text自动清理幂等"""
    assert F('h3 a').text.build() == {
        'sel': 'h3 a', 'get': 'text', 'all': False, 'global': False,
        'steps': [{'op': 'strip'}, {'op': 'collapse'}]}
    # .text重复访问只保留一份自动清理
    assert F('h3 a').text.text.build()['steps'] == [{'op': 'strip'}, {'op': 'collapse'}]
    # .text_raw与切换到attr后无自动清理
    assert F('h3 a').text_raw.build()['steps'] == []
    assert F('a').attr('href').build() == {
        'sel': 'a', 'get': 'attr:href', 'all': False, 'global': False, 'steps': []}
    assert F('a').prop('checked').build()['get'] == 'prop:checked'
    assert F('.tag', all=True).html.build()['all'] is True
    assert F.global_('#total').text.int().build()['global'] is True
    # 链式与等价dict经normalize后一致
    from dp_core.collect import _normalize_dict
    chained = F('a').text.build()
    dicts = _normalize_dict('x', {'sel': 'a', 'get': 'text',
                                  'steps': [{'op': 'strip'}, {'op': 'collapse'}]})
    chained.pop('name', None)
    assert chained == dicts
    print('ok  F构建与两形态等价')


def test_processors():
    """各处理器：正常值/None/列表/失败转换"""
    s = _apply_steps
    assert s('  a \n b ', [{'op': 'strip'}]) == 'a \n b'
    assert s('  a \n b ', [{'op': 'collapse'}]) == ' a b '
    assert s(' a\nb ', [{'op': 'strip'}, {'op': 'collapse'}]) == 'a b'
    assert s('a b\tc\n', [{'op': 'remove_blank'}]) == 'abc'
    assert s('a-b', [{'op': 'replace', 'old': '-', 'new': '_'}]) == 'a_b'
    assert s('a1b22', [{'op': 'replace', 'old': r'\d+', 'new': 'N', 'regex': True}]) == 'aNbN'
    assert s('第3页', [{'op': 're', 'pattern': r'(\d+)', 'group': 1}]) == '3'
    assert s('第3页', [{'op': 're', 'pattern': r'\d+', 'group': 0}]) == '3'
    assert s('abc', [{'op': 're', 'pattern': r'(\d+)'}]) is None  # 无匹配
    assert s('x', [{'op': 'prefix', 'value': '前'}, {'op': 'suffix', 'value': '后'}]) == '前x后'
    assert s('42', [{'op': 'int'}]) == 42
    assert s(' 42 ', [{'op': 'int'}]) == 42
    assert s('abc', [{'op': 'int'}]) is None
    assert s('12.0', [{'op': 'int'}]) is None  # 需先float
    assert s('12.0', [{'op': 'float'}, {'op': 'int'}]) == 12
    assert s('3.14', [{'op': 'float'}]) == 3.14
    assert s('x', [{'op': 'map', 'fn': lambda v: v * 2}]) == 'xx'
    assert s(['a', 'b'], [{'op': 'join', 'sep': '-'}]) == 'a-b'
    assert s('x', [{'op': 'join', 'sep': '-'}]) == 'x'  # 非列表按str拼接
    # None传播：default之外全跳过；default后恢复管道
    assert s(None, [{'op': 'strip'}, {'op': 're', 'pattern': r'(\d)'}]) is None
    assert s(None, [{'op': 'default', 'value': 'N'}, {'op': 'prefix', 'value': '['}]) == '[N'
    assert s(None, [{'op': 'default', 'value': 5}, {'op': 'int'}]) == 5
    # 列表逐元素：元素级None保持
    assert s([' a ', None], [{'op': 'strip'}]) == ['a', None]
    assert s(['1', 'x'], [{'op': 'int'}]) == [1, None]
    # 全链组合
    v = s('  价格: 1,234 元 ', [{'op': 'collapse'}, {'op': 'replace', 'old': ',', 'new': ''},
                               {'op': 're', 'pattern': r'(\d+)'}, {'op': 'int'}])
    assert v == 1234, v
    print('ok  处理器管道')


def test_param_validation():
    """collect参数校验：不触达run_js即抛ValueError"""

    class FakeTab:  # run_js若被调用即失败
        def run_js(self, *a, **kw):
            raise AssertionError('参数校验失败时应提前返回，不触达run_js')

    t = FakeTab()
    for bad_items in ('', None, 123):
        try:
            _collect_raw(t, bad_items, {'a': F('x').text})
            raise AssertionError(f'items={bad_items!r} 应抛ValueError')
        except ValueError:
            pass
    for bad_fields in ({}, None, 'x', {'a': 'x'}):
        try:
            _collect_raw(t, '.it', bad_fields)
            raise AssertionError(f'fields={bad_fields!r} 应抛ValueError')
        except ValueError:
            pass
    try:
        _collect_raw(t, '.it', {'a': {'get': 'text'}})  # 缺sel
        raise AssertionError('dict缺sel应抛ValueError')
    except ValueError:
        pass
    try:
        _collect_raw(t, '.it', {'a': {'sel': 'x', 'get': 'bad'}})
        raise AssertionError('非法get应抛ValueError')
    except ValueError:
        pass
    try:
        F.global_('')  # 全局字段必须给选择器
        raise AssertionError('全局字段空选择器应抛ValueError')
    except ValueError:
        pass
    try:
        _collect_raw(t, '.it', {'a': F.global_('')})
        raise AssertionError('全局字段空选择器应抛ValueError')
    except ValueError:
        pass
    print('ok  参数校验')


# ---------------- 集成测试（远程浏览器） ----------------
def test_on_browser(addr):
    browser = Chromium(addr)
    print(f'已连接：{browser.address}  {browser.version}')
    tab = browser.new_tab()
    try:
        # 注入固定列表页
        tab.run_js('document.body.innerHTML = arguments[0]', LIST_HTML)

        data = tab.collect('#list .it', {
            'title': F('a').text,                                # 默认strip+collapse
            'raw': F('a').text_raw,                              # 原始文本
            'url': F('a').attr('href').prefix('https://x.com'),  # 属性+前缀
            'id': F('a').attr('data-id').int(),                  # 属性缺失→None
            'num': F('a').attr('data-id').int().default(0),      # default兜底
            'tags': F('.tag', all=True).text.join('-'),          # all多值拼接
            'nope': F('.nope').text,                             # 元素不存在→None
            'total': F.global_('#total').text.re(r'(\d+)'),      # 全局字段
        })

        assert len(data) == 3, f'应3条记录，收到{len(data)}'
        assert list(data[0].keys()) == ['title', 'raw', 'url', 'id', 'num', 'tags', 'nope', 'total']
        assert data[0]['title'] == '标题一', data[0]['title']
        assert '\n' in data[0]['raw'] or '  ' in data[0]['raw'], 'text_raw应保留原始空白'
        assert data[0]['url'] == 'https://x.com/p/1'
        assert data[0]['id'] == 1 and data[0]['num'] == 1
        assert data[0]['tags'] == '甲-乙'
        assert data[1]['title'] == '标题 二', data[1]['title']  # 连续空格合一
        assert data[1]['tags'] == '丙'
        assert data[2]['id'] is None and data[2]['nope'] is None
        assert data[2]['num'] == 0, data[2]['num']  # None→default(0)
        assert data[2]['tags'] == ''  # all无匹配→空列表join为空串
        assert data[2]['total'] == '23' and data[0]['total'] == '23'
        for r in data:
            assert r['total'] == '23', '全局字段应填充进每条记录'

        # items无匹配→空列表
        assert tab.collect('#nope .it', {'a': F('a').text}) == []

        # 选择器语法错误→JavaScriptError
        from dp_core.errors import JavaScriptError
        try:
            tab.collect('#list .it', {'a': F('###bad###').text})
            raise AssertionError('选择器语法错误应抛JavaScriptError')
        except JavaScriptError as e:
            assert '###bad###' in str(e), f'错误消息应含选择器：{e}'

        # dict形态与F形态等价（attr走dict、默认get为text原始）
        d2 = tab.collect('#list .it', {
            'url': {'sel': 'a', 'get': 'attr:href'},
            'title': {'sel': 'a', 'get': 'text',
                      'steps': [{'op': 'strip'}, {'op': 'collapse'}]},
        })
        assert d2[0]['url'] == '/p/1' and d2[0]['title'] == '标题一'
        assert d2 == [{'url': r['url'].replace('https://x.com', ''),
                       'title': r['title']} for r in data]

        # 通讯次数：collect取数只产生一次evaluate（钩住run_cdp计数）
        calls = {'n': 0}
        orig = tab.run_cdp

        def counting(cmd, **kw):
            if cmd == 'Runtime.evaluate':
                calls['n'] += 1
            return orig(cmd, **kw)

        tab.run_cdp = counting
        tab.collect('#list .it', {'t': F('a').text})
        tab.run_cdp = orig
        assert calls['n'] == 1, f'取数应只1次evaluate，实际{calls["n"]}次'
        print(f'ok  批量采集：{len(data)}条记录，取数单次通讯')
    finally:
        tab.close()
        browser.disconnect()


def main():
    test_f_build()
    test_processors()
    test_param_validation()
    addr = sys.argv[1] if len(sys.argv) > 1 else ADDR
    test_on_browser(addr)
    print('\ncollect 自测全部通过。')


if __name__ == '__main__':
    main()
