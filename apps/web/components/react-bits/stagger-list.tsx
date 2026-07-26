"use client";

import { motion, useReducedMotion } from "motion/react";
import { Children, type ReactNode } from "react";

// 改编自 React Bits AnimatedList（MIT + Commons Clause）：
// https://reactbits.dev/components/animated-list
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
