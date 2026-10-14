# -*- coding: utf-8 -*-
"""给 coze/tiktok/jimeng 的 verify 加 account_type 提取。"""
import io
import re

# 1) import sniff_account_type
for f in ['src/channels/coze.py', 'src/channels/tiktok.py', 'src/channels/jimeng.py']:
    s = io.open(f, encoding='utf-8').read()
    if 'sniff_account_type' not in s:
        s = re.sub(r'from \.base import \(([^)]+)\)',
                   lambda m: 'from .base import (' + m.group(1).replace(
                       'VerifyResult,', 'VerifyResult, sniff_account_type,'),
                   s, count=1)
        if 'sniff_account_type' not in s:
            s = s.replace('from .base import VerifyResult',
                          'from .base import VerifyResult, sniff_account_type')
        io.open(f, 'w', encoding='utf-8').write(s)
        print(f, 'import 已加')

# 2) verify 返回处加 account_type
pairs = [
    ('src/channels/coze.py',
     'return VerifyResult(ok=ok, status="active" if ok else "invalid", raw=redact(r))',
     'return VerifyResult(ok=ok, status="active" if ok else "invalid",\n'
     '                                account_type=sniff_account_type(r) if ok else "", raw=redact(r))'),
    ('src/channels/tiktok.py',
     'return VerifyResult(ok=ok, status="active" if ok else "invalid", raw=redact(r))',
     'return VerifyResult(ok=ok, status="active" if ok else "invalid",\n'
     '                                account_type=sniff_account_type(r) if ok else "", raw=redact(r))'),
    ('src/channels/jimeng.py',
     'return VerifyResult(ok=ok, status="active" if ok else "invalid",\n'
     '                                message="" if ok else str(r)[:200], raw=redact(r))',
     'return VerifyResult(ok=ok, status="active" if ok else "invalid",\n'
     '                                account_type=sniff_account_type(r) if ok else "",\n'
     '                                message="" if ok else str(r)[:200], raw=redact(r))'),
]
for f, old, new in pairs:
    s = io.open(f, encoding='utf-8').read()
    if old in s:
        s = s.replace(old, new, 1)
        io.open(f, 'w', encoding='utf-8').write(s)
        print(f, 'verify 已加 account_type')
    else:
        print(f, 'SKIP 未找到（可能已改）')
