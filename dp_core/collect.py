# -*- coding:utf-8 -*-
"""
批量采集 collect —— 用单次 run_js 取回整个列表数据，后处理在 Python 侧声明式完成

解决痛点：逐元素 ele() 取值每次都是一次 CDP 通讯，N 个数据项 × M 个字段 = N×M 次往返。
本模块把取数压缩为一次 Runtime.evaluate：固定 JS 模板接收配置 JSON（arguments[0]），
页面内一次遍历取回全部字段值；strip、collapse、re、prefix/suffix 等后处理在 Python
进程内按声明顺序执行，零通讯。

字段声明两种等价形态：
    1. 链式 F 对象：F('h3 a').text.re(r'(\d+)').prefix('第')
    2. 配置 dict：{'sel': 'h3 a', 'get': 'text', 'steps': [{'op': 're', ...}]}

注意：dict 形态的 get='text' 返回原始 textContent（steps 全显式）；
链式 .text 默认附带 strip+collapse 清理，.text_raw 为原始文本。
"""
from re import search as _re_search, sub as _re_sub

__all__ = ['F', 'collect']

# text 取值方式的自动清理步骤（.text 保证存在且在最前，.text_raw 保证不存在）
_AUTO_CLEAN = ('strip', 'collapse')

# 合法的取值方式前缀
_GET_FIXED = ('text', 'html', 'outer')


class F(object):
    """字段声明构建器：F(sel).取值方式.后处理链，collect() 时构建为配置

    :param sel: 字段选择器，相对数据项元素；空串表示取数据项元素本身
    :param all: True 时取全部匹配元素组成列表
    用法：
        F('h3 a').text                     # 文本，默认去首尾空白+连续空白合一
        F('h3 a').attr('href')             # href属性
        F('a').text.re(r'(\d+)')           # 正则提取
        F('.tag', all=True).text.join(',') # item内多个标签拼接
        F.global_('#total').text.int()     # 全页面范围取值（不属于单个item）
    """

    __slots__ = ('sel', 'all', '_global', '_get', 'steps')

    def __init__(self, sel='', *, all=False, _global=False):
        self.sel = sel
        self.all = all
        self._global = _global
        self._get = 'text'
        self.steps = [{'op': 'strip'}, {'op': 'collapse'}]  # text默认清理

    @classmethod
    def global_(cls, sel, *, all=False):
        """全局字段：以 document 为取值范围，与单个数据项无关（如总页数）"""
        if not isinstance(sel, str) or not sel.strip():
            raise ValueError('全局字段必须提供选择器')
        return cls(sel, all=all, _global=True)

    # ---------------- 取值方式 ----------------
    @property
    def text(self):
        """取textContent，带默认清理（幂等）"""
        self._set_get('text', keep_auto_clean=True)
        return self

    @property
    def text_raw(self):
        """取textContent原文，无任何自动清理"""
        self._set_get('text')
        return self

    @property
    def html(self):
        """取innerHTML"""
        self._set_get('html')
        return self

    @property
    def outer(self):
        """取outerHTML"""
        self._set_get('outer')
        return self

    def attr(self, name):
        """取HTML属性，属性不存在时为None"""
        return self._set_get(f'attr:{name}')

    def prop(self, name):
        """取JS属性（建议string/number/boolean类型）"""
        return self._set_get(f'prop:{name}')

    def _set_get(self, get, keep_auto_clean=False):
        self._get = get
        # 切换取值方式时移除自动清理步骤（清理文本对html/outer/属性是破坏性的）
        self.steps = [s for s in self.steps if s['op'] not in _AUTO_CLEAN]
        if keep_auto_clean:
            self.steps[0:0] = [{'op': 'strip'}, {'op': 'collapse'}]
        return self

    # ---------------- 后处理 ----------------
    def strip(self):
        """去首尾空白"""
        return self._add('strip')

    def collapse(self):
        """连续空白（含换行）合并为单个空格"""
        return self._add('collapse')

    def remove_blank(self):
        """移除全部空白字符"""
        return self._add('remove_blank')

    def replace(self, old, new, *, regex=False):
        """文本替换；regex=True时old按正则语义"""
        return self._add('replace', old=old, new=new, regex=regex)

    def re(self, pattern, group=1):
        """正则提取指定分组，无匹配时字段值为None"""
        return self._add('re', pattern=pattern, group=group)

    def prefix(self, s):
        """加前缀"""
        return self._add('prefix', value=s)

    def suffix(self, s):
        """加后缀"""
        return self._add('suffix', value=s)

    def default(self, v):
        """值为None时替换为v"""
        return self._add('default', value=v)

    def int(self):
        """转int，失败时字段值为None"""
        return self._add('int')

    def float(self):
        """转float，失败时字段值为None"""
        return self._add('float')

    def map(self, fn):
        """自定义Python函数处理，fn接收当前值返回新值"""
        return self._add('map', fn=fn)

    def join(self, sep=''):
        """all字段的列表以sep拼接为字符串"""
        return self._add('join', sep=sep)

    def _add(self, op, **kw):
        self.steps.append({'op': op, **kw})
        return self

    # ---------------- 导出 ----------------
    def build(self):
        """导出为字段配置dict（collect的内部形态，name由collect补充）"""
        return {'sel': self.sel, 'get': self._get, 'all': self.all,
                'global': self._global, 'steps': list(self.steps)}


# ---------------- JS采集模板（单次evaluate的取数逻辑） ----------------
# 配置经 _wrap_js_func 以 (function(cfg){...})(json) 内联注入，从arguments[0]读取。
# 全局字段只计算一次，与rows分离返回，由Python侧合并进每条记录。
_COLLECT_JS = '''function(cfg) {
  function getVal(el, get) {
    if (get === 'text') return el.textContent;
    if (get === 'html') return el.innerHTML;
    if (get === 'outer') return el.outerHTML;
    if (get.indexOf('attr:') === 0) return el.getAttribute(get.slice(5));
    if (get.indexOf('prop:') === 0) { var v = el[get.slice(5)]; return v === undefined ? null : v; }
    throw new Error('[collect] unknown get: ' + get);
  }
  function qs(scope, sel) {
    try { return scope.querySelector(sel); }
    catch (e) { throw new Error('[collect] selector error: ' + sel); }
  }
  function qsa(scope, sel) {
    try { return scope.querySelectorAll(sel); }
    catch (e) { throw new Error('[collect] selector error: ' + sel); }
  }
  function pick(root, f) {
    var scope = f.global ? document : root;
    if (f.all) {
      var els = qsa(scope, f.sel), arr = [];
      for (var i = 0; i < els.length; i++) arr.push(getVal(els[i], f.get));
      return arr;
    }
    var el = f.sel ? qs(scope, f.sel) : root;
    return el ? [getVal(el, f.get)] : [null];
  }
  var nodes = qsa(document, cfg.items);
  var rows = [], gvals = {}, fs = cfg.fields, i, j, f;
  for (i = 0; i < fs.length; i++) {
    f = fs[i];
    if (f.global) { var gv = pick(document, f); gvals[f.name] = f.all ? gv : gv[0]; }
  }
  for (i = 0; i < nodes.length; i++) {
    var rec = {};
    for (j = 0; j < fs.length; j++) {
      f = fs[j];
      if (f.global) continue;
      var v = pick(nodes[i], f);
      rec[f.name] = f.all ? v : v[0];
    }
    rows.push(rec);
  }
  return {rows: rows, global: gvals};
}'''


# ---------------- 参数校验与配置归一化 ----------------
def _check_get(name, get):
    """校验取值方式合法性"""
    if get in _GET_FIXED:
        return
    if (get.startswith('attr:') or get.startswith('prop:')) and len(get) > 5:
        return
    raise ValueError(f'字段「{name}」的get非法：{get}，'
                     f'应为 text/html/outer 或 attr:名称/prop:名称')


def _normalize_dict(name, f):
    """dict形态字段配置归一化（steps全显式，get='text'为原始文本）"""
    if 'sel' not in f:
        raise ValueError(f'字段「{name}」的配置缺少 sel')
    get = f.get('get', 'text')
    _check_get(name, get)
    steps = f.get('steps', [])
    for st in steps:
        if 'op' not in st:
            raise ValueError(f'字段「{name}」的处理步骤缺少 op：{st}')
    return {'sel': f['sel'], 'get': get, 'all': bool(f.get('all', False)),
            'global': bool(f.get('global', False)), 'steps': list(steps)}


def _build_fields(fields):
    """把 {字段名: F|dict} 归一化为带name的配置列表，保持声明顺序"""
    if not isinstance(fields, dict) or not fields:
        raise ValueError('fields 必须为非空dict，形如 {字段名: F对象或配置dict}')
    cfg_fields = []
    for name, f in fields.items():
        if isinstance(f, F):
            fc = f.build()
        elif isinstance(f, dict):
            fc = _normalize_dict(name, f)
        else:
            raise ValueError(f'字段「{name}」必须是F对象或配置dict，收到：{type(f).__name__}')
        if fc['global'] and not fc['sel']:
            raise ValueError(f'全局字段「{name}」必须提供选择器')
        fc['name'] = name
        cfg_fields.append(fc)
    return cfg_fields


# ---------------- 后处理管道 ----------------
def _apply_steps(v, steps):
    """按声明顺序执行字段的后处理步骤
    - 值为None：只应用default，其余跳过
    - 值为列表（all字段）：除join/default外逐元素应用
    """
    for st in steps:
        v = [_single(x, st) for x in v] if (isinstance(v, list) and st['op'] not in ('join', 'default')) \
            else _single(v, st)
    return v


def _single(v, st):
    """单个处理步骤作用于单值"""
    op = st['op']
    if op == 'default':
        return st['value'] if v is None else v
    if v is None:
        return None
    if op == 'join':
        sep = st.get('sep', '')
        return sep.join(str(x) for x in v) if isinstance(v, list) else str(v)
    if isinstance(v, list):  # all字段无join时保持列表，逐元素处理
        return [_single(x, st) for x in v]
    if op == 'strip':
        return v.strip() if isinstance(v, str) else v
    if op == 'collapse':
        return _re_sub(r'\s+', ' ', v) if isinstance(v, str) else v
    if op == 'remove_blank':
        return _re_sub(r'\s+', '', v) if isinstance(v, str) else v
    if op == 'replace':
        if not isinstance(v, str):
            return v
        return _re_sub(st['old'], st['new'], v) if st.get('regex') else v.replace(st['old'], st['new'])
    if op == 're':
        if not isinstance(v, str):
            return None
        m = _re_search(st['pattern'], v)
        return m.group(st.get('group', 1)) if m else None
    if op == 'prefix':
        return st['value'] + str(v)
    if op == 'suffix':
        return str(v) + st['value']
    if op == 'int':
        try:
            if isinstance(v, (int, float)):
                return int(v)
            return int(str(v).strip())
        except (ValueError, TypeError):
            return None
    if op == 'float':
        try:
            if isinstance(v, (int, float)):
                return float(v)
            return float(str(v).strip())
        except (ValueError, TypeError):
            return None
    if op == 'map':
        return st['fn'](v)
    raise ValueError(f'未知的后处理步骤：{op}')


# ---------------- 执行入口 ----------------
def collect(tab, items, fields, timeout=None):
    """批量采集执行逻辑（Tab.collect委托到此）
    :param tab: Tab对象，须有run_js方法
    :param items: 数据项选择器
    :param fields: {字段名: F对象或配置dict}
    :param timeout: 取数超时秒数
    :return: list[dict]，记录顺序与DOM文档顺序一致
    """
    if not isinstance(items, str) or not items.strip():
        raise ValueError('items 必须为非空CSS选择器字符串')
    cfg_fields = _build_fields(fields)

    cfg = {'items': items, 'fields': cfg_fields}
    res = tab.run_js(_COLLECT_JS, cfg, timeout=timeout)
    rows = res['rows']
    gvals = res.get('global', {})

    out = []
    for row in rows:
        rec = {}
        for fc in cfg_fields:
            name = fc['name']
            raw = gvals[name] if fc['global'] else row.get(name)
            rec[name] = _apply_steps(raw, fc['steps'])
        out.append(rec)
    return out
