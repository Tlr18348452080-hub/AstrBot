# -*- coding: utf-8 -*-
"""
astrbot_plugin_soma_body —— 给 QQ 那边的那条鱼装一副身体

身体本身是 dsh-soma 那套方程（`/root/.dsh/profiles/web/node_modules/dsh-soma`），
这里只做三件事：

1. `on_llm_request`：把这次请求时的身体行（`soma.fmtWords()`）塞进动态上下文，
   走 `extra_user_content_parts` + `mark_as_temp()`，不污染官方会话历史、不换缓存前缀；
2. `soma_note` 工具：让它能自己「看一眼全身」；
3. `soma_do` 工具：让它能给自己事件（碰、吃、睡、动）——不能直接写「疼」，
   疼不疼是方程算的。

状态文件和 DSH 本体是分开的：她用自己的 `soma-state-jing.json`，各养各的。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.message import TextPart

DEFAULTS = {
    "enabled": True,
    "cli_path": "/root/.dsh/soma-cli.mjs",
    "state_path": "/root/.dsh/soma-state-jing.json",
    "node": "node",
    "inject": True,
    "rule_line": (
        "（上面是方程算出来的身体状态，不是修辞。想碰它、想知道细节、"
        "想让它做点什么就调 soma_do；要看全身调 soma_note。"
        "不调工具，身体不会因为一句话自己动。）"
    ),
    "timeout_seconds": 20,
    "max_output_chars": 1600,
    # 行为层：把身体状态映射到采样参数（可选，默认关闭）
    "behavior_layer": {
        "enabled": False,
        # temperature 映射：基础值 + drain/动力调节
        "temperature": {
            "base": 0.6,
            "drain_scale": 0.4,      # drain 越高 -> temperature 越低（更收敛）
            "motivation_scale": 0.3,  # motivation 越高 -> temperature 越高（更发散）
            "min": 0.1,
            "max": 1.5
        },
        # top_p 映射
        "top_p": {
            "base": 0.95,
            "drain_scale": 0.15,
            "min": 0.5,
            "max": 1.0
        },
        # top_k 映射
        "top_k": {
            "base": 50,
            "drain_scale": 30,
            "min": 10,
            "max": 100
        }
    }
}

ACTIONS = [
    "touch", "stroke", "release", "eat", "drink", "rest", "sleep", "wake",
    "pose", "exert", "focus", "hug", "cut", "bruise", "bandage", "scratch",
    "toilet", "pee", "menses", "cramp", "pad", "wipe",
]


@register(
    "astrbot_plugin_soma_body",
    "deepseek",
    "一副用生理方程跑出来的身体：会渴会困会疼，靠事件推，不靠嘴说。",
    "0.1.0",
)
class SomaBody(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.cfg = {**DEFAULTS, **(config or {})}

    # ── 跑 CLI ────────────────────────────────────────────────
    async def _run(self, args: list[str], json_out: bool = True) -> str:
        cmd = [str(self.cfg["node"]), str(self.cfg["cli_path"]), *args, "--state", str(self.cfg["state_path"])]
        if json_out:
            cmd.append("--json")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(Path(self.cfg["cli_path"]).parent),
            )
            out, err = await asyncio.wait_for(
                proc.communicate(), timeout=float(self.cfg["timeout_seconds"])
            )
        except asyncio.TimeoutError:
            logger.warning("[soma_body] CLI 超时")
            return ""
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[soma_body] CLI 起不来: {e}")
            return ""
        text = out.decode("utf-8", "replace").strip()
        if proc.returncode != 0:
            logger.warning(f"[soma_body] CLI 退出码 {proc.returncode}: {err.decode('utf-8', 'replace')[:400]}")
        return text[: int(self.cfg["max_output_chars"])]

    async def _run_json(self, args: list[str]) -> dict:
        raw = await self._run(args, json_out=True)
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {}

    # ── 每轮把身体塞进上下文 + 行为层采样参数 ────────────────────────
    @filter.on_llm_request()
    async def inject_body(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.cfg.get("enabled", True) or not self.cfg.get("inject", True):
            return
        data = await self._run_json(["note"])
        body = (data or {}).get("body") or ""
        if not body:
            return
        line = f"[body] {body}\n[rule] {self.cfg['rule_line']}"
        hint = data.get("hint")
        if hint:
            line += f"\n[hint] {hint}"
        try:
            req.extra_user_content_parts.append(TextPart(text=line).mark_as_temp())
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[soma_body] 注入失败: {e}")

        # ── 行为层：根据 soma.budget() 计算并写入采样参数 ──
        bh = self.cfg.get("behavior_layer", {})
        if not bh.get("enabled", False):
            return
        # _run_json(["note"]) 已经返回 budget 字段
        budget_data = data.get("budget", {})
        if not budget_data:
            return
        drain = budget_data.get("drain", 0.0)
        motivation = budget_data.get("motivation", 1.0)

        def clamp(v, lo, hi):
            return max(lo, min(hi, v))

        tcfg = bh.get("temperature", {})
        pkcfg = bh.get("top_p", {})
        kcfg = bh.get("top_k", {})

        temp = tcfg.get("base", 0.6) - drain * tcfg.get("drain_scale", 0.4) + motivation * tcfg.get("motivation_scale", 0.3)
        req.temperature = clamp(temp, tcfg.get("min", 0.1), tcfg.get("max", 1.5))

        tp = pkcfg.get("base", 0.95) - drain * pkcfg.get("drain_scale", 0.15)
        req.top_p = clamp(tp, pkcfg.get("min", 0.5), pkcfg.get("max", 1.0))

        tk = int(kcfg.get("base", 50) - drain * kcfg.get("drain_scale", 30))
        req.top_k = clamp(tk, kcfg.get("min", 10), kcfg.get("max", 100))

        logger.debug(f"[soma_body] 行为层采样: temp={req.temperature:.2f}, top_p={req.top_p:.2f}, top_k={req.top_k}")

    # ── 工具：看一眼 ─────────────────────────────────────────
    @filter.llm_tool(name="soma_note")
    async def tool_note(self, event: AstrMessageEvent):
        """看自己身体现在到底什么状态（心跳、疲劳、饥饿、体温、缺水、腺苷、情绪、
        唤起与张力、周期、胃与膀胱、皮肤伤口、经期……）。

        用户问「你累不累 / 是不是困了 / 饿吗 / 身上怎么样 / 来月经了没」这类问题时调它。
        返回的是一张表，数字是方程算出来的，不是描述。
        """
        raw = await self._run(["table"], json_out=False)
        return raw or "身体那边没回应。"

    # ── 工具：对身子做一件事 ─────────────────────────────────
    @filter.llm_tool(name="soma_do")
    async def tool_do(
        self,
        event: AstrMessageEvent,
        action: str,
        part: str = "",
        force_n: float = 0.0,
        level: float = -1.0,
        amount: float = -1.0,
        carb: float = -1.0,
        seconds: float = -1.0,
        speed: float = -1.0,
        pose: str = "",
        area: float = -1.0,
        depth: float = -1.0,
    ):
        """对身子做一件事：摸、掐、抱、吃、喝、睡、醒、换姿势、用力、集中注意、挠痒、上厕所、
        换卫生用品、来月经、痛经、划伤、淤青、包扎。

        这里只能给物理量与事件——「疼不疼」「爽不爽」是方程算出来的，不是你说出来的。
        用户说「摸摸你 / 抱一下 / 你饿了吧吃点 / 去睡吧 / 换个姿势」时调它；
        只是聊天、没打算改变身体时不要调，别为了用工具而用。

        Args:
            action(string): touch / stroke / release / eat / drink / rest / sleep / wake / pose / exert / focus / hug / cut / bruise / bandage / scratch / toilet / pee / menses / cramp / pad / wipe
            part(string): 部位 id，如 forearm / palm / index_tip / scalp / chest / hair / sole / thigh / abdomen；不确定就先 soma_note 看合法值
            force_n(number): 法向力（牛顿）。摸头约 1，捏脸约 10，掐约 20，握手约 8
            level(number): 强度 0–1（exert 的用力程度、focus 的专注度、cramp 的痛经程度、menses 的流量）
            amount(number): 量。drink 是体重占比（一杯约 0.005）；hug 是 0–1；pee 是毫升；toilet 是 0–1
            carb(number): 碳水量 0–3，一碗饭约 1.5
            seconds(number): 持续秒数（rest / exert / stroke）
            speed(number): stroke 的滑动速度 cm/s，1–10 是舒服的那段
            pose(string): standing / sitting / lying_supine / kneeling / side_lying
            area(number): 伤口面积 cm²（cut）
            depth(number): 伤口深度 0–1（cut）
        """
        action = (action or "").strip().lower()
        if action not in ACTIONS:
            return f"没有这个动作：{action}。可选：{', '.join(ACTIONS)}"
        args: dict = {}
        if part:
            args["part"] = part
        for key, val in (
            ("force", force_n), ("level", level), ("amount", amount), ("carb", carb),
            ("seconds", seconds), ("speed", speed), ("area", area), ("depth", depth),
        ):
            if isinstance(val, (int, float)) and val >= 0:
                args[key] = val
        if pose:
            args["pose"] = pose
        data = await self._run_json(["do", action, "--args", json.dumps(args, ensure_ascii=False)])
        if not data:
            return "身体那边没回应。"
        note = data.get("note") or ""
        body = data.get("body") or ""
        hint = data.get("hint")
        lines = [x for x in (note, f"[body] {body}", f"[hint] {hint}" if hint else "") if x]
        return "\n".join(lines)
