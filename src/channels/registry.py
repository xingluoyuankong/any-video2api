# -*- coding: utf-8 -*-
"""渠道注册表：从 config/channels/*.json 自动发现并实例化渠道适配器。

描述符里通过 `adapter` 字段指向实现类，例如：
    { "adapter": "src.channels.jimeng:JimengChannel", ... }

约定：描述符 name == 渠道名；未写 adapter 时按 `src.channels.<name>:<Name>Channel` 猜测。
"""
from __future__ import annotations

import importlib
from typing import Dict, List, Optional

from src.core.config import config
from src.channels.base import Channel, ChannelSpec


class ChannelRegistry:
    def __init__(self):
        self._channels: Dict[str, Channel] = {}
        self._specs: Dict[str, ChannelSpec] = {}
        self._errors: Dict[str, str] = {}

    def load(self) -> "ChannelRegistry":
        self._channels.clear()
        self._specs.clear()
        self._errors.clear()
        enabled = config.enabled_channels
        for name, raw in config.channel_specs().items():
            if enabled is not None and name not in enabled:
                continue
            try:
                spec = ChannelSpec.from_dict(name, raw)
                cls = self._resolve_class(name, raw)
                self._specs[name] = spec
                self._channels[name] = cls(spec)
            except Exception as exc:  # 单渠道加载失败不影响其它渠道
                self._errors[name] = f"{type(exc).__name__}: {exc}"
        return self

    @staticmethod
    def _resolve_class(name: str, raw: dict):
        target = raw.get("adapter")
        if not target:
            target = f"src.channels.{name}:{name.capitalize()}Channel"
        module_name, _, class_name = target.partition(":")
        module = importlib.import_module(module_name)
        cls = getattr(module, class_name)
        if not issubclass(cls, Channel):
            raise TypeError(f"{target} 不是 Channel 子类")
        return cls

    # ---- 查询 ----
    def get(self, name: str) -> Optional[Channel]:
        return self._channels.get(name)

    def require(self, name: str) -> Channel:
        ch = self._channels.get(name)
        if ch is None:
            raise KeyError(f"渠道未注册: {name}（已注册: {list(self._channels)}）")
        return ch

    def names(self) -> List[str]:
        return list(self._channels.keys())

    def specs(self) -> Dict[str, ChannelSpec]:
        return dict(self._specs)

    def describe(self) -> List[dict]:
        out = []
        for name, ch in self._channels.items():
            out.append({
                "name": name,
                "display_name": ch.spec.display_name or name,
                "kind": ch.spec.kind,
                "base_url": ch.spec.base_url,
                "transports": ch.spec.transports,
                "capabilities": ch.capabilities(),
                "model_count": len(ch.spec.models or {}),
            })
        for name, err in self._errors.items():
            out.append({"name": name, "error": err, "capabilities": {}})
        return out

    def errors(self) -> Dict[str, str]:
        return dict(self._errors)


registry = ChannelRegistry().load()
