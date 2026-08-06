# -*- coding: utf-8 -*-
"""WebUI process resource sampling."""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from typing import Any, Dict

_resource_lock = threading.Lock()
_resource_sample = {
    "wall": time.time(),
    "cpu": time.process_time(),
    "percent": 0.0,
}
_psutil_proc = None
_net_connections_cache: Dict[str, Any] = {"value": 0, "ts": 0.0}
_open_files_cache: Dict[str, Any] = {"value": 0, "ts": 0.0}


def format_uptime(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}天 {hours}小时"
    if hours:
        return f"{hours}小时 {minutes}分钟"
    if minutes:
        return f"{minutes}分钟 {secs}秒"
    return f"{secs}秒"


def _get_memory_usage_mb(proc: Any = None) -> float:
    try:
        import psutil  # type: ignore
        if proc is None:
            proc = psutil.Process(os.getpid())
        return round(proc.memory_info().rss / 1024 / 1024, 1)
    except Exception:
        pass
    try:
        if sys.platform == "win32":
            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_uint32),
                    ("PageFaultCount", ctypes.c_uint32),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]
            kernel32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            psapi.GetProcessMemoryInfo.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
                ctypes.c_uint32,
            ]
            psapi.GetProcessMemoryInfo.restype = ctypes.c_int
            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(counters)
            if psapi.GetProcessMemoryInfo(
                kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
            ):
                return round(counters.WorkingSetSize / 1024 / 1024, 1)
        else:
            import resource
            usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
            return round(usage / divisor, 1)
    except Exception:
        pass
    return 0.0


def get_resource_usage() -> Dict[str, Any]:
    global _psutil_proc
    try:
        import psutil  # type: ignore
        if _psutil_proc is None:
            _psutil_proc = psutil.Process(os.getpid())
            _psutil_proc.cpu_percent(interval=None)
        percent = round(_psutil_proc.cpu_percent(interval=None), 1)
        cpu_count = psutil.cpu_count(logical=True) or 1
        system_percent = round(psutil.cpu_percent(interval=None), 1)
        normalized_percent = round(percent / max(cpu_count, 1), 1)
        info = _psutil_proc.memory_info()
        usage = {
            "cpu_percent": normalized_percent,
            "cpu_percent_normalized": normalized_percent,
            "cpu_percent_process": percent,
            "cpu_percent_system": system_percent,
            "cpu_count": cpu_count,
            "memory_mb": round(info.rss / 1024 / 1024, 1),
        }
        try:
            now = time.time()
            if now - _net_connections_cache["ts"] > 10:
                _net_connections_cache["value"] = len(_psutil_proc.net_connections(kind="inet"))
                _net_connections_cache["ts"] = now
            now2 = time.time()
            if now2 - _open_files_cache["ts"] > 10:
                _open_files_cache["value"] = len(_psutil_proc.open_files())
                _open_files_cache["ts"] = now2
            usage.update({
                "rss_mb": round(info.rss / 1024 / 1024, 1),
                "vms_mb": round(info.vms / 1024 / 1024, 1),
                "threads": _psutil_proc.num_threads(),
                "open_files": _open_files_cache["value"],
                "connections": _net_connections_cache["value"],
            })
        except Exception:
            pass
        return usage
    except Exception:
        pass

    now = time.time()
    cpu_now = time.process_time()
    with _resource_lock:
        prev_wall = float(_resource_sample.get("wall") or now)
        prev_cpu = float(_resource_sample.get("cpu") or cpu_now)
        elapsed = max(now - prev_wall, 0.001)
        cpu_delta = max(cpu_now - prev_cpu, 0.0)
        percent = round(min(100.0, (cpu_delta / elapsed) * 100.0), 1)
        cpu_count = os.cpu_count() or 1
        normalized_percent = round(percent / max(cpu_count, 1), 1)
        _resource_sample.update({"wall": now, "cpu": cpu_now, "percent": normalized_percent})
    return {
        "cpu_percent": normalized_percent,
        "cpu_percent_normalized": normalized_percent,
        "cpu_percent_process": percent,
        "cpu_count": cpu_count,
        "memory_mb": _get_memory_usage_mb(),
    }
