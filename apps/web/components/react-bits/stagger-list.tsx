"use client";

import { motion, useReducedMotion } from "motion/react";
import { Children, type ReactNode } from "react";

// 上游参考：React Bits AnimatedList，HEAD 61909958（2026-07-29 核对）：
// https://reactbits.dev/components/animated-list
// 本地改动：只保留一次性进入动效，并在 reduced-motion 下直接静态渲染。
type StaggerListProps = {
  delay?: number;
  className?: string;
  children: ReactNode;
};

// motion 的 transition 读不到 CSS 变量，这里复写 --ease-standard 的数值
const EASE_STANDARD: [number, number, number, number] = [0.2, 0.8, 0.2, 1];

export function StaggerList({ delay = 60, className, children }: Readonly<StaggerListProps>) {
  const reducedMotion = useReducedMotion();

  return (
    <div className={className}>
      {Children.map(children, (child, index) =>
        child == null ? (
          child
        ) : (
          <motion.div
            className="stagger-item"
            initial={reducedMotion ? false : { opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            transition={
              reducedMotion
                ? { duration: 0 }
                : {
                    // 单项时长对齐 --duration-pop，逐项延迟默认 60ms（1f 动效规范）
                    duration: 0.24,
                    delay: (index * delay) / 1000,
                    ease: EASE_STANDARD,
                  }
            }
          >
            {child}
          </motion.div>
        ),
      )}
    </div>
  );
}
