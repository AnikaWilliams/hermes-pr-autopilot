"""Minimal test-only process-tree cleanup helper."""

from __future__ import annotations

import psutil


def kill_process_tree(pid: int) -> None:
    """Terminate a fixture process and its descendants."""

    try:
        parent = psutil.Process(pid)
    except psutil.Error:
        return
    processes = parent.children(recursive=True) + [parent]
    for process in processes:
        try:
            process.terminate()
        except psutil.Error:
            continue
    _gone, alive = psutil.wait_procs(processes, timeout=3)
    for process in alive:
        try:
            process.kill()
        except psutil.Error:
            continue
