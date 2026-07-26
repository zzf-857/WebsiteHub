"use client";

import { useReducedMotion } from "motion/react";

// Adapted from React Bits ShinyText (MIT + Commons Clause):
// https://reactbits.dev/text-animations/shiny-text
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
