"use client";

import { Check } from "lucide-react";
import { motion, useReducedMotion } from "motion/react";

import { Spinner } from "./spinner";

// 上游风格参考：React Bits，HEAD 61909958（2026-07-29 核对）：
// https://reactbits.dev/
// 本地改动：WebHub 自有 Spinner/Check 状态组合、固定弹入曲线与 reduced-motion。
type PopCheckProps = {
  done: boolean;
  size?: number;
};

// motion 的 transition 读不到 CSS 变量，这里复写 --ease-standard 的数值
const EASE_STANDARD: [number, number, number, number] = [0.2, 0.8, 0.2, 1];

export function PopCheck({ done, size = 16 }: Readonly<PopCheckProps>) {
  const reducedMotion = useReducedMotion();

  // 未完成时展示加载中；Spinner 自带 reduced-motion 降级。
  // 不透传 size：设计稿里对勾 14px、加载圆环 13px 是两个尺寸，
  // 圆环撑到 14 会比落定后的对勾还大，看起来像先胀后缩。
  if (!done) {
    return <Spinner />;
  }

  if (reducedMotion) {
    return (
      <span className="pop-check" aria-hidden="true">
        <Check size={size} strokeWidth={2} />
      </span>
    );
  }

  return (
    <motion.span
      className="pop-check"
      aria-hidden="true"
      initial={{ scale: 0.6, opacity: 0 }}
      // 弹入曲线 0.6→1.08→1，总时长对齐 --duration-pop（240ms）
      animate={{ scale: [0.6, 1.08, 1], opacity: [0, 1, 1] }}
      transition={{ duration: 0.24, times: [0, 0.7, 1], ease: EASE_STANDARD }}
    >
      <Check size={size} strokeWidth={2} />
    </motion.span>
  );
}
