"use client";

import { useReducedMotion } from "motion/react";

// 上游风格参考：React Bits，HEAD 61909958（2026-07-29 核对）：
// https://reactbits.dev/
// 本地改动：WebHub CSS 令牌、调用方语义、可变尺寸与 reduced-motion 省略号降级。
type SpinnerProps = {
  size?: number;
  className?: string;
};

export function Spinner({ size = 13, className }: Readonly<SpinnerProps>) {
  const reducedMotion = useReducedMotion();

  // 系统要求减弱动效时退化为静态省略号，而不是一个不转的圆环——
  // 静止的圆环看不出"正在加载"，省略号反而语义更清楚；
  // 语义（role / aria-label）交给调用方补充，这里统一对读屏隐藏。
  if (reducedMotion) {
    return (
      <span
        className={className ? `spinner-static ${className}` : "spinner-static"}
        style={{ fontSize: size }}
        aria-hidden="true"
      >
        …
      </span>
    );
  }

  // 设计稿 1b/1e 的加载指示是纯 CSS 圆环（默认 13px）：
  // 形状、颜色与旋转都在 motion.css 的 .spinner 里，这里只按 size 定尺寸
  return (
    <span
      className={className ? `spinner ${className}` : "spinner"}
      style={{ width: size, height: size }}
      aria-hidden="true"
    />
  );
}
