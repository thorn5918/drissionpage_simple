# -*- coding:utf-8 -*-
"""
工具函数（提取自 DrissionPage/_functions/tools.py）
"""
from time import perf_counter, sleep

from .errors import CDPError, PageDisconnectedError


def port_is_using(ip, port):
    """检查端口是否被占用"""
    from socket import socket, AF_INET, SOCK_STREAM
    s = socket(AF_INET, SOCK_STREAM)
    s.settimeout(.1)
    result = s.connect_ex((ip, int(port)))
    s.close()
    return result == 0


def wait_until(function, kwargs=None, timeout=10):
    """轮询执行function直到返回真值或超时（超时抛TimeoutError）"""
    if kwargs is None:
        kwargs = {}
    end_time = perf_counter() + timeout
    while perf_counter() < end_time:
        value = function(**kwargs)
        if value:
            return value
        sleep(.01)
    raise TimeoutError


def is_js_func(func):
    """判断script是否为完整的函数声明（来自原库 _functions/web.py）"""
    func = func.strip()
    return (func.startswith('function') or func.startswith('async ')) and func.endswith('}')


def raise_error(result, ignore=None):
    """把 Driver.run() 返回的错误 dict 转成异常抛出（精简自原库同名函数，保留主干分支）
    :param result: Driver.run() 返回的错误dict
    :param ignore: 异常类或异常类元组，匹配时静默返回不抛出
    """
    error = result.get('error', '')
    if error in ('connection disconnected', 'No target with given id found',
                 'Inspected target navigated or closed', 'Cannot find context with specified id'):
        r = PageDisconnectedError(f'连接已断开：{result.get("method", "")}')
    elif result.get('type') == 'timeout':
        r = TimeoutError(f'CDP命令无响应：{result.get("method")}，参数：{result.get("args")}')
    elif result.get('type') == 'alert_exists':
        r = RuntimeError(f'存在未处理的alert，命令被跳过：{result.get("method")}')
    elif result.get('type') == 'connection_error':
        r = PageDisconnectedError(f'连接已断开：{error}')
    else:
        r = CDPError(f'CDP命令执行失败：{result.get("method")}，错误：{error}，'
                     f'参数：{result.get("args")}，详情：{result.get("data")}')

    if not ignore or not isinstance(r, ignore):
        raise r
