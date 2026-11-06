# -*- coding: utf-8 -*-
"""
CDP WebSocket 驱动（提取自 DrissionPage/_base/driver.py，逻辑保持一致，仅剥离多语言与告警外无关依赖）

核心职责：
1. 与某个调试目标建立 WebSocket 长连接：
   - 浏览器级：ws://{address}/devtools/browser/{browser_id}
   - 页面级：  ws://{address}/devtools/page/{tab_id}
2. 后台线程持续接收消息：命令响应按 id 回填，事件投递到队列
3. run() 发送 CDP 命令并同步等待响应
"""
from json import dumps, loads, JSONDecodeError
from queue import Queue, Empty
from threading import Thread
from time import perf_counter, sleep

from requests import Session, adapters
from websocket import (WebSocketTimeoutException, WebSocketConnectionClosedException, create_connection,
                       WebSocketException, WebSocketBadStatusException)

from .errors import BrowserConnectError

adapters.DEFAULT_RETRIES = 5

# CDP 命令默认超时（秒），可用 run_cdp(cmd, _timeout=x) 覆盖
CDP_TIMEOUT = 30


class Driver(object):
    """对应一个 CDP 调试目标的 WebSocket 连接"""

    def __init__(self, target_id, target_type, address, owner=None):
        self.id = target_id
        self.type = target_type  # 'browser' 或 'page'
        self.address = address  # '127.0.0.1:9222'
        self.owner = owner
        self.alert_flag = False  # 标记alert出现，跳过一条请求后复原

        self._websocket_url = f'ws://{address}/devtools/{target_type}/{target_id}'
        self._cur_id = 0
        self._ws = None

        self._recv_th = Thread(target=self._recv_loop)
        self._handle_event_th = Thread(target=self._handle_event_loop)
        self._recv_th.daemon = True
        self._handle_event_th.daemon = True

        self.is_running = False

        self.event_handlers = {}  # {事件名: 回调}
        self.method_results = {}  # {命令id: 结果Queue}
        self.event_queue = Queue()

        self.start()

    def _send(self, message, timeout=None):
        """发送一条 CDP 命令并等待响应到达 method_results"""
        self._cur_id += 1
        ws_id = self._cur_id
        message['id'] = ws_id
        message_json = dumps(message)

        end_time = perf_counter() + timeout if timeout is not None else None
        self.method_results[ws_id] = Queue()
        try:
            self._ws.send(message_json)
            if timeout == 0:  # 不等待返回
                self.method_results.pop(ws_id, None)
                return {'id': ws_id, 'result': {}}

        except (OSError, WebSocketConnectionClosedException):
            self.method_results.pop(ws_id, None)
            return {'error': {'message': 'connection disconnected'}, 'type': 'connection_error'}

        while self.is_running:
            try:
                result = self.method_results[ws_id].get(timeout=.2)
                self.method_results.pop(ws_id, None)
                return result

            except Empty:
                if self.alert_flag and message['method'].startswith(('Input.', 'Runtime.')):
                    return {'error': {'message': 'alert exists.'}, 'type': 'alert_exists'}

                if timeout is not None and perf_counter() > end_time:
                    self.method_results.pop(ws_id, None)
                    return {'error': {'message': 'alert exists.'}, 'type': 'alert_exists'} \
                        if self.alert_flag else {'error': {'message': 'timeout'}, 'type': 'timeout'}

                continue

        return {'error': {'message': 'connection disconnected'}, 'type': 'connection_error'}

    def _recv_loop(self):
        """后台接收线程：响应按id分发，事件入队"""
        while self.is_running:
            try:
                msg_json = self._ws.recv()
                msg = loads(msg_json)
            except WebSocketTimeoutException:
                continue
            except (WebSocketException, OSError, WebSocketConnectionClosedException, JSONDecodeError):
                self._stop()
                return

            if 'method' in msg:  # 事件
                if msg['method'].startswith('Page.javascriptDialog'):
                    self.alert_flag = msg['method'].endswith('Opening')
                self.event_queue.put(msg)

            elif msg.get('id') in self.method_results:  # 命令响应
                self.method_results[msg['id']].put(msg)

    def _handle_event_loop(self):
        """后台事件处理线程：按事件名调用回调"""
        while self.is_running:
            try:
                event = self.event_queue.get(timeout=1)
            except Empty:
                continue

            for function in self.event_handlers.get(event['method'], ()):
                function(**event['params'])

            self.event_queue.task_done()

    def run(self, _method, **kwargs):
        """执行cdp方法
        :param _method: cdp方法名，如 'Page.navigate'
        :param kwargs: cdp参数
        :param _timeout: 超时时间（秒），None为无限等待，0为不等待返回
        :return: 执行结果，成功返回 {'result': {...}} 中的 result，失败返回 {'error': ..., 'type': ...}
        """
        if not self.is_running:
            return {'error': 'connection disconnected', 'type': 'connection_error'}

        timeout = kwargs.pop('_timeout', CDP_TIMEOUT)
        result = self._send({'method': _method, 'params': kwargs}, timeout=timeout)
        if 'result' not in result and 'error' in result:
            kwargs['_timeout'] = timeout
            return {'error': result['error']['message'], 'type': result.get('type', 'call_method_error'),
                    'method': _method, 'args': kwargs, 'data': result['error'].get('data')}
        else:
            return result['result']

    def start(self):
        self.is_running = True
        try:
            self._ws = create_connection(self._websocket_url, enable_multithread=True, suppress_origin=True)
        except WebSocketBadStatusException as e:
            if 'Handshake status 403 Forbidden' in str(e):
                raise EnvironmentError('websocket-client版本过低，请升级：pip install -U websocket-client')
            else:
                return
        except ConnectionRefusedError:
            raise BrowserConnectError(f'无法连接 {self._websocket_url}，浏览器不存在或已关闭。')
        self._recv_th.start()
        self._handle_event_th.start()
        return True

    def stop(self):
        self._stop()
        while self._recv_th.is_alive():
            sleep(.01)
        return True

    def _stop(self):
        if not self.is_running:
            return False

        self.is_running = False
        if self._ws:
            self._ws.close()
            self._ws = None

        self.event_handlers.clear()
        self.method_results.clear()
        self.event_queue.queue.clear()

        if hasattr(self.owner, '_on_disconnect'):
            self.owner._on_disconnect()

    def set_callback(self, event, callback):
        """注册/取消 CDP 事件回调（同一事件可注册多个回调，互不覆盖）
        :param event: 事件名，如 'Network.responseReceived'
        :param callback: 回调函数，接收事件params作为关键字参数；传None取消该事件的所有回调
        """
        if callback:
            handlers = self.event_handlers.setdefault(event, [])
            if callback not in handlers:
                handlers.append(callback)
        else:
            self.event_handlers.pop(event, None)


class BrowserDriver(Driver):
    """浏览器级驱动，同一个browser_id全局只保留一个连接（与原库一致）"""
    BROWSERS = {}

    def __new__(cls, target_id, target_type, address, owner):
        if target_id in cls.BROWSERS:
            return cls.BROWSERS[target_id]
        return object.__new__(cls)

    def __init__(self, target_id, target_type, address, owner):
        if hasattr(self, '_created'):
            return
        self._created = True
        BrowserDriver.BROWSERS[target_id] = self
        super().__init__(target_id, target_type, address, owner)

    def __repr__(self):
        return f'<BrowserDriver {self.id}>'

    @staticmethod
    def get(url):
        """独立的http请求（原库用它获取/json/list标签页清单，顺序与界面一致）"""
        s = Session()
        s.trust_env = False
        s.keep_alive = False
        r = s.get(url, headers={'Connection': 'close'})
        r.close()
        s.close()
        return r
