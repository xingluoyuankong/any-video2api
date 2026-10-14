# -*- coding: utf-8 -*-
"""批量修渠道 verify：把「网络/限流异常」从 invalid 改为 risk（不降级账号）。"""
import io

OLD = '''        except UpstreamError as e:
            return VerifyResult(ok=False, status="invalid", message=str(e))'''
NEW = '''        except UpstreamError as e:
            s = str(e)
            # 只有明确鉴权失败才算凭证失效；网络/限流/风控算临时错误（risk，不降级）
            if "401" in s or "未登录" in s or "not logged" in s.lower():
                return VerifyResult(ok=False, status="invalid", message=s)
            return VerifyResult(ok=False, status="risk", message=s)'''

for f in ['src/channels/coze.py', 'src/channels/tiktok.py', 'src/channels/jimeng.py']:
    s = io.open(f, encoding='utf-8').read()
    if OLD in s:
        s = s.replace(OLD, NEW, 1)
        io.open(f, 'w', encoding='utf-8').write(s)
        print(f, 'OK 已修')
    else:
        print(f, 'SKIP 模式未找到')
