"""只读跨服务聚合，明确缺失挂载；文件导出也使用相同脱敏器。"""
from collections import deque
from pathlib import Path
from common.utils.logging_utils import redact_secrets,log_context

SERVICES=('backend-web','websocket','scheduler')

def collect_logs(root: Path, *, correlation=None, level=None, service=None, limit=100, offset=0):
    limit=max(1,min(limit,1000));offset=max(0,min(offset,100000))
    states={}; collected=[];total=0
    for name in SERVICES:
        directory=root/name/'logs'
        if not directory.is_dir():states[name]='missing';continue
        states[name]='available'
        if service and service!=name:continue
        recent=deque(maxlen=limit+offset)
        for file in sorted(directory.glob('*.log')):
            if file.is_symlink() or file.name.startswith('error'):continue
            try:
                with file.open(encoding='utf8',errors='replace') as stream:
                    for line in stream:
                        if not line.strip() or (correlation and correlation not in line) or (level and f'| {level.upper()}' not in line):continue
                        total+=1;recent.append(f'[{name}] '+redact_secrets(line.rstrip()))
            except OSError:states[name]='unreadable'
        collected.extend(recent)
    # 文件本身保留接收顺序；跨服务展示附带来源，不伪造全局严格时序。
    right=len(collected)-offset
    return {'success':True,'logs':collected[max(0,right-limit):max(0,right)],'total':total,'services':states,'order':'service_then_file','retention_days':30}

from common.middleware.correlation import CorrelationMiddleware
