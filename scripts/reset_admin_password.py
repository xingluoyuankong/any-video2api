# -*- coding: utf-8 -*-
"""reset_admin_password.py — 重置控制台管理员密码（更复杂）

用法：PYTHONPATH=. python scripts/reset_admin_password.py
输出：新密码写入 data/initial_admin.txt（权限 600），并吊销全部旧会话
"""
import os
import secrets
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.platform.users import UserStore, INITIAL_ADMIN_FILE  # noqa: E402

# 密码字符集：大小写 + 数字 + 符号（排除易混淆的 0O1lI|）
UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"
LOWER = "abcdefghijkmnpqrstuvwxyz"
DIGIT = "23456789"
SYMBOL = "!@#$%^&*-_=+?"


def gen_password(length: int = 20) -> str:
    pools = [UPPER, LOWER, DIGIT, SYMBOL]
    # 每类至少 2 个
    chars = [secrets.choice(p) for p in pools for _ in range(2)]
    allc = "".join(pools)
    chars += [secrets.choice(allc) for _ in range(length - len(chars))]
    # 洗牌（Fisher-Yates with secrets）
    for i in range(len(chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        chars[i], chars[j] = chars[j], chars[i]
    return "".join(chars)


def main() -> None:
    store = UserStore()
    username = os.getenv("AV2A_ADMIN_USER", "admin")

    user = store.get(username)
    if not user:
        print("未找到用户 %s，现有用户：" % username)
        for u in store.list():
            print("  -", u.get("username"))
        return

    new_pwd = gen_password(20)
    ok = store.reset_password(username, new_pwd)
    if not ok:
        print("重置失败")
        return

    # 吊销全部旧会话（旧 cookie 立即失效）
    revoked = store.revoke_user_sessions(user["id"])
    print("已吊销旧会话:", revoked, "个")

    # 写文件
    p = Path(INITIAL_ADMIN_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"用户名: {username}\n密码:   {new_pwd}\n"
        f"（重置于脚本 reset_admin_password.py，请妥善保管）\n",
        encoding="utf-8",
    )
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass

    # 校验新密码可登录
    if store.verify(username, new_pwd):
        print("=== 重置成功，新密码已验证 ===")
    else:
        print("!!! 校验失败，请检查")
    print("用户名:", username)
    print("密码:  ", new_pwd)
    print("已写入:", INITIAL_ADMIN_FILE)


if __name__ == "__main__":
    main()
