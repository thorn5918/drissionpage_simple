# dp_core —— DrissionPage 核心能力抽取

从 [DrissionPage](https://gitee.com/g1879/DrissionPage) 抽取的最小可运行核心，只保留两件事：

1. **通过 remote-debugging-port 接管浏览器**（纯连接模式，不查找、不启动本地浏览器）
2. **发送 CDP 命令**（浏览器级 + 页面级），并支持订阅 CDP 事件

依赖只有 `requests` 和 `websocket-client`。

> 注：原仓库 dev 分支处于重构中间态（`__init__.py` 引用的 `_browsers` 模块不存在，import 即失败），
> 本抽取以 master 分支的稳定实现为蓝本，核心机制与 dev 分支一致。

## 文件与原库对应关系

| 抽取文件 | 原库文件 | 内容 |
|---|---|---|
| `dp_core/driver.py` | `_base/driver.py` | WebSocket CDP 驱动（`Driver`/`BrowserDriver`）：命令按 id 收发、事件后台分发 |
| `dp_core/launcher.py` | `_functions/browser.py` | 调试端口连接/校验（`parse_address`/`check_connect`） |
| `dp_core/browser.py` | `_base/chromium.py` + `_pages/chromium_base.py` | `Chromium` 浏览器对象与 `Tab` 标签页对象 |
| `dp_core/tools.py` | `_functions/tools.py` | `port_is_using`、`raise_error` 等工具 |
| `dp_core/errors.py` | `errors.py` | `BrowserConnectError`/`CDPError`/`PageDisconnectedError` |

方法名与原库保持一致（`run_cdp`、`_run_cdp`、`set_callback`、`Target.createTarget` 用法等），
方便对照原库源码做二次开发。

## 安装

```bash
pip install -r requirements.txt
```

## 快速开始

```python
from dp_core import Chromium

# 地址支持 'ip:port'、'http://ip:port/json/list'、int端口
browser = Chromium('123.168.144.158:20001')

# 浏览器级 CDP
r = browser.run_cdp('Browser.getVersion')
print(r['product'], r['userAgent'])

# 页面级 CDP
tab = browser.new_tab(url='https://www.baidu.com')
r = tab.run_cdp('Runtime.evaluate', expression='document.title', returnByValue=True)
print(r['result']['value'])

# 执行JS（有返回值）
tab.run_js('return document.title')                      # 函数体，return返回值
tab.run_js('function(a, b){return a + b}', 1, 2)         # 函数声明 + 参数
tab.run_js('document.title', as_expr=True)               # 表达式
tab.run_js('return fetch("/api").then(r => r.json())')   # Promise会被等待

# 直接访问接口url拿JSON
data = tab.json('https://xxx/api/xxx')   # 导航到接口地址并解析
data = tab.json()                        # 或解析当前页面内容

# 订阅 CDP 事件
urls = []
tab.on('Network.requestWillBeSent', lambda requestId, request, **kw: urls.append(request['url']))
tab.run_cdp('Network.enable')

# 断开连接（浏览器保持运行）；browser.quit() 才会关闭远端浏览器
browser.disconnect()
```

## 运行演示与测试

```bash
# 演示：连接、CDP 命令、事件订阅
python demo.py http://123.168.144.158:20001/json/list

# 自测：地址解析、连接失败报错、完整 CDP 流程
python test_dp_core.py http://123.168.144.158:20001/json/list
```

## 关键设计（与原库一致的行为）

- **WebSocket 地址自行拼接**：`ws://{address}/devtools/browser/{browser_id}` 或
  `ws://{address}/devtools/page/{tab_id}`。`/json/version` 返回的 `webSocketDebuggerUrl`
  里的 host 可能与实际可达地址不一致（经过代理/端口映射时会丢端口），
  因此只取其最后一段作为 `browser_id`/`tab_id`，连接地址按 `address` 重建。
- **同一浏览器复用同一 Chromium 对象**：按 `browser_id` 单例，重复 `Chromium(addr)` 返回同一实例。
- **标签页清单走 HTTP 接口**（`/json/list`），与浏览器界面顺序一致；原库注释明确"不要改用cdp，因为顺序不对"。
- **事件回调是多播列表**：同一事件可注册多个回调互不覆盖；`Chromium` 内部依赖
  `Target.targetCreated/Destroyed` 维护标签页表，用户回调与内部回调并存。
- **命令超时**：`run_cdp(cmd, _timeout=秒)`，默认 30 秒；`_timeout=0` 只发不等。

## run_js 与 json 的实现说明

- `run_js` 对应原库 `_elements/chromium_element.py` 的 `run_js`，简化点：
  统一用 `Runtime.evaluate(returnByValue=True, awaitPromise=True, userGesture=True)`，
  函数体自动包成 `(function(){...})()`、参数 JSON 序列化注入（等价原库 `Runtime.callFunctionOn`
  传参）；返回值经 returnByValue 直接反序列化成 Python 对象，无需原库 `parse_js_result`
  的元素对象分支（dp_core 没有元素体系）。`Infinity/NaN` 等不可序列化值单独映射，
  序列化失败的大对象回退 `JSON.stringify`。
- `tab.json()` 用 `document.body.innerText` 取页面渲染出的 JSON 文本再 `json.loads`；
  Chrome 把 `application/json` 响应渲染在 `<pre>` 里，innerText 即原始 JSON。
  当前页面不是 JSON 时抛 `ValueError` 并给出开头片段。

## 二次开发建议

- 加 CDP 能力：直接 `tab.run_cdp('Page.captureScreenshot', ...)` 等即可，无需改库。
- 加 DOM/元素操作：参考原库 `_pages/chromium_base.py` 的 `ele()`/`eles()` 实现，
  底层就是 `Runtime.evaluate` / `DOM.*` 命令。
- 加下载/拦截：`Browser.setDownloadBehavior`、`Fetch.*` 命令，事件走 `tab.on()`。
