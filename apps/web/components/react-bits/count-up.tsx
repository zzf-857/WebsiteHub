"use client";

import { useReducedMotion } from "motion/react";
import { useEffect, useRef, useState } from "react";

// 改编自 React Bits CountUp（MIT + Commons Clause）：
// https://reactbits.dev/text-animations/count-up
// 1f 规范只要求"计数变化脉冲一下"，不做逐位滚动。
type CountUpProps = {
  value: number;
  className?: string;
};

export function CountUp({ value, className }: Readonly<CountUpProps>) {
  const reducedMotion = useReducedMotion();
  const previous = useRef(value);
  // 每次数值变化 +1，通过 key 重挂载元素来重放 CSS 脉冲动画
  const [pulseTick, setPulseTick] = useState(0);

  useEffect(() => {
    if (previous.current !== value) {
      previous.current = value;
      setPulseTick((tick) => tick + 1);
    }
  }, [value]);

  // 首帧不脉冲，只有数值真正变化过才播；reduced-motion 时永远静态
  const shouldPulse = !reducedMotion && pulseTick > 0;

  return (
    <span
      key={pulseTick}
      className={className ? `count-up ${className}` : "count-up"}
      data-pulse={shouldPulse || undefined}
    >
      {value}
    </span>
  );
}
