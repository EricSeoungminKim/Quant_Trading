"""EventSink 구현: JSONL append / 콘솔 출력 / 다중 sink 브로드캐스트."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from quant.core.models import Fill, Signal


class JsonlSink:
    def __init__(self, path: str | Path = "data/log/events.jsonl"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def on_signal(self, signal: Signal) -> None:
        self._append("signal", signal)

    def on_fill(self, fill: Fill) -> None:
        self._append("fill", fill)

    def _append(self, kind: str, obj) -> None:
        row = {"type": kind, **asdict(obj)}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")


class ConsoleSink:
    def on_signal(self, signal: Signal) -> None:
        print(f"[SIGNAL] {signal.symbol} {signal.action.value} {signal.reason}", flush=True)

    def on_fill(self, fill: Fill) -> None:
        print(
            f"[FILL] {fill.symbol} {fill.side.value} {fill.qty:g}@{fill.price:.2f} {fill.reason}",
            flush=True,
        )


class MultiSink:
    def __init__(self, sinks: list):
        self.sinks = sinks

    def on_signal(self, signal: Signal) -> None:
        for s in self.sinks:
            s.on_signal(signal)

    def on_fill(self, fill: Fill) -> None:
        for s in self.sinks:
            s.on_fill(fill)

    def on_order(self, state) -> None:
        """`on_order` 를 가진 하위 sink 에만 전달한다.

        **이게 없으면 체인 안쪽의 주문 소비자가 영원히 굶는다** — 루프는
        `isinstance(sinks, OrderSink)` 로 물어보므로 중간 래퍼가 전달하지 않으면
        판정이 거기서 끊긴다(2026-08-14 배선 중 실제로 그랬다).
        """
        for s in self.sinks:
            fn = getattr(s, "on_order", None)
            if fn is not None:
                fn(state)
