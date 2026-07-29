"use client";

import { motion, useInView, useReducedMotion } from "motion/react";
import { useEffect, useRef } from "react";

// Upstream reference: React Bits BlurText, HEAD 61909958 (checked 2026-07-29).
// https://reactbits.dev/text-animations/blur-text
// Local changes: character-level aria labeling, one-shot viewport trigger and reduced-motion fallback.
type BlurTextProps = {
  text: string;
  className?: string;
  delay?: number;
  onAnimationComplete?: () => void;
};

export function BlurText({
  text,
  className,
  delay = 45,
  onAnimationComplete,
}: Readonly<BlurTextProps>) {
  const ref = useRef<HTMLSpanElement>(null);
  const inView = useInView(ref, { amount: 0.45, once: true });
  const reducedMotion = useReducedMotion();
  const characters = Array.from(text);

  useEffect(() => {
    if ((inView || reducedMotion) && onAnimationComplete) {
      const duration = reducedMotion ? 0 : characters.length * delay + 320;
      const timer = window.setTimeout(onAnimationComplete, duration);
      return () => window.clearTimeout(timer);
    }
  }, [characters.length, delay, inView, onAnimationComplete, reducedMotion]);

  return (
    <span ref={ref} className={className} aria-label={text}>
      {characters.map((character, index) => (
        <motion.span
          aria-hidden="true"
          key={`${character}-${index}`}
          initial={reducedMotion ? false : { filter: "blur(8px)", opacity: 0, y: 5 }}
          animate={
            inView || reducedMotion
              ? { filter: "blur(0px)", opacity: 1, y: 0 }
              : { filter: "blur(8px)", opacity: 0, y: 5 }
          }
          transition={{ duration: reducedMotion ? 0 : 0.32, delay: reducedMotion ? 0 : index * delay / 1000 }}
          style={{ display: "inline-block", whiteSpace: "pre" }}
        >
          {character}
        </motion.span>
      ))}
    </span>
  );
}
