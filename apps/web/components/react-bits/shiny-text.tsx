"use client";

import { useReducedMotion } from "motion/react";

// Upstream reference: React Bits ShinyText, HEAD 61909958 (checked 2026-07-29).
// https://reactbits.dev/text-animations/shiny-text
// Local changes: CSS-token palette, configurable duration and reduced/forced-colors fallbacks.
type ShinyTextProps = {
  text: string;
  className?: string;
  speed?: number;
};

export function ShinyText({ text, className, speed = 3 }: Readonly<ShinyTextProps>) {
  const reducedMotion = useReducedMotion();
  return (
    <span
      className={className ? `shiny-text ${className}` : "shiny-text"}
      data-static={reducedMotion || undefined}
      style={reducedMotion ? undefined : { animationDuration: `${speed}s` }}
    >
      {text}
    </span>
  );
}
