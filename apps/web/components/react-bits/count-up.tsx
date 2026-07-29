"use client";

import { useReducedMotion } from "motion/react";
import { useEffect, useRef, useState } from "react";

// 上游参考：React Bits CountUp，HEAD 61909958（2026-07-29 核对）：
// https://reactbits.dev/text-animations/count-up
// 本地改动：加入节流数值过渡、变化脉冲、aria-live 与 reduced-motion 静态降级；
// 保持单个等宽数字，不做逐位滚动。
type CountUpProps = {
  value: number;
  className?: string;
  durationMs?: number;
};

export function CountUp({ value, className, durationMs = 320 }: Readonly<CountUpProps>) {
  const reducedMotion = useReducedMotion();
  const previous = useRef(value);
  const displayedRef = useRef(value);
  const [displayed, setDisplayed] = useState(value);
  // 每次数值变化 +1，通过 key 重挂载元素来重放 CSS 脉冲动画
  const [pulseTick, setPulseTick] = useState(0);

  useEffect(() => {
    if (previous.current !== value) {
      previous.current = value;
      setPulseTick((tick) => tick + 1);
    }
  }, [value]);

  useEffect(() => {
    if (reducedMotion || durationMs <= 0) {
      displayedRef.current = value;
      const frame = window.requestAnimationFrame(() => setDisplayed(value));
      return () => window.cancelAnimationFrame(frame);
    }
    const from = displayedRef.current;
    const startedAt = performance.now();
    let lastPaintAt = 0;
    let frame = 0;
    const tick = (now: number) => {
      const progress = Math.min(1, (now - startedAt) / durationMs);
      if (now - lastPaintAt >= 50 || progress === 1) {
        const eased = 1 - (1 - progress) ** 3;
        const next = Math.round(from + (value - from) * eased);
        if (next !== displayedRef.current) {
          displayedRef.current = next;
          setDisplayed(next);
        }
        lastPaintAt = now;
      }
      if (progress < 1) frame = window.requestAnimationFrame(tick);
    };
    frame = window.requestAnimationFrame(tick);
    return () => window.cancelAnimationFrame(frame);
  }, [durationMs, reducedMotion, value]);

  // 首帧不脉冲，只有数值真正变化过才播；reduced-motion 时永远静态
  const shouldPulse = !reducedMotion && pulseTick > 0;

  return (
    <span
      key={pulseTick}
      className={className ? `count-up ${className}` : "count-up"}
      data-pulse={shouldPulse || undefined}
      aria-live="polite"
      aria-atomic="true"
      aria-label={value.toLocaleString("zh-CN")}
    >
      <span aria-hidden="true">{displayed.toLocaleString("zh-CN")}</span>
    </span>
  );
}
