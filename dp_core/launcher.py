# -*- coding:utf-8 -*-
"""
调试端口连接逻辑（提取自 DrissionPage/_functions/browser.py 的"接管已有浏览器"分支）

只做一件事：连接/校验一个已开启 --remote-debugging-port 的浏览器调试端口。
绝不查找、启动本地浏览器进程。
"""
from time import perf_counter, sleep

from requests import Session

from .errors import BrowserConnectError
from .tools import port_is_using

# 等待调试端口就绪的超时（秒）
BROWSER_CONNECT_TIMEOUT = 30


def parse_address(addr_or_port=None):
    """规整化地址为 'ip:port' 形式（对应原库 handle_options + connect_browser 的地址清洗）
    :param addr_or_port: 'http://ip:port/json/list'、'ip:port'、int端口，None时用默认 127.0.0.1:9222
    :return: 'ip:port'
    """
    if addr_or_port is None:
        return '127.0.0.1:9222'
    if isinstance(addr_or_port, int):
        if addr_or_port <= 0:
            raise ValueError(f'端口必须为正整数：{addr_or_port}')
        return f'127.0.0.1:{addr_or_port}'

    address = str(addr_or_port).strip().replace('localhost', '127.0.0.1')
    if '://' in address:  # 去掉 http(s)/ws(s) 前缀
        address = address.split('://', 1)[1]
    address = address.split('/', 1)[0]  # 去掉 /json/list 之类的路径
    if ':' not in address:
        raise ValueError(f'地址必须包含端口：{addr_or_port}，应为 ip:port 形式。')
    return address


def check_connect(address, timeout=BROWSER_CONNECT_TIMEOUT):
    """校验调试端口是否为可控制的浏览器
    :param address: 'ip:port'
    :param timeout: 等待就绪的秒数
    :return: /json/version 的 dict（含 Browser、User-Agent、webSocketDebuggerUrl 等）
    """
    ip, port = address.split(':')

    s = Session()
    s.trust_env = False
    s.keep_alive = False

    def do():
        try:
            r = s.get(f'http://{ip}:{port}/json/version', timeout=10, headers={'Connection': 'close'})
            return r.json() if 'webSocketDebuggerUrl' in r.json() else None
        except Exception:
            return None

    end_time = perf_counter() + timeout
    info = None
    while perf_counter() < end_time:
        info = do()
        if info:
            break
        sleep(.1)
    s.close()

    if not info:
        hint = (f'端口 {port} 被占用但不是浏览器调试端口' if port_is_using(ip, port)
                else f'无法连接 {address}')
        raise BrowserConnectError(
            f'{hint}。请确认浏览器以 --remote-debugging-port={port} 启动，且防火墙允许该端口访问。')
    return info
