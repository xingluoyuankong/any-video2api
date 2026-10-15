# -*- coding: utf-8 -*-
"""修三个一致性缺陷：
1) CreditView 缺 message 字段 -> 渠道 credits() 写 view.message 会 AttributeError
2) hailuo/kling/vidu 的 poll 签名用 **kw，缺 req -> jobs.py 以位置参数传 req 会 TypeError
3) jimeng.py 用了不存在的 req.image -> AttributeError
"""
import io
import re

# --- 1) CreditView 加 message 字段 ---
p = 'src/channels/base.py'
s = io.open(p, encoding='utf-8').read()
old = '''class CreditView:
    total: float = 0.0
    allocatable: float = 0.0
    buckets: Dict[str, float] = field(default_factory=dict)
    error: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)'''
new = '''class CreditView:
    total: float = 0.0
    allocatable: float = 0.0
    buckets: Dict[str, float] = field(default_factory=dict)
    error: str = ""
    message: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)'''
if old in s:
    s = s.replace(old, new, 1)
    io.open(p, 'w', encoding='utf-8').write(s)
    print('base.py: CreditView 已加 message 字段')
else:
    print('base.py: SKIP（可能已加）')

# --- 2) poll 签名统一 ---
for f in ['src/channels/hailuo.py', 'src/channels/kling.py', 'src/channels/vidu.py']:
    s = io.open(f, encoding='utf-8').read()
    old = 'def poll(self, secret: AccountSecret, upstream_task_id: str, **kw: Any) -> Any:'
    new = ('def poll(self, secret: AccountSecret, upstream_task_id: str,\n'
           '                    req: Optional["GenerateRequest"] = None,\n'
           '                    **kw: Any) -> Any:')
    if old in s:
        s = s.replace(old, new, 1)
        io.open(f, 'w', encoding='utf-8').write(s)
        print(f, 'poll 签名已统一')
    else:
        print(f, 'poll SKIP')

# --- 3) jimeng req.image ---
p = 'src/channels/jimeng.py'
s = io.open(p, encoding='utf-8').read()
old = 'images = req.images or ([req.image] if req.image else [])'
new = ("images = list(req.images or [])\n"
           "    if not images:\n"
           "        _single = getattr(req, \"image\", \"\") or getattr(req, \"image_url\", \"\")\n"
           "        if _single:\n"
           "            images = [_single]")
if old in s:
    s = s.replace(old, new, 1)
    io.open(p, 'w', encoding='utf-8').write(s)
    print('jimeng.py: req.image 已改为 getattr 兼容')
else:
    print('jimeng.py: SKIP')
