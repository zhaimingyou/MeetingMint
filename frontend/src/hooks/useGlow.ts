import { useCallback } from "react";

/**
 * Sets the CSS custom properties (``--mx`` / ``--my``) that drive the
 * pointer-following radial glow on ``.card`` elements (see ``global.css``).
 *
 * Respects ``prefers-reduced-motion``: when the user has requested reduced
 * motion the handler is a no-op so no glow is painted.
 */
export function useGlow() {
  return useCallback((e: React.MouseEvent<HTMLElement>) => {
    if (
      typeof window !== "undefined" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches
    ) {
      return;
    }
    const el = e.currentTarget;
    const r = el.getBoundingClientRect();
    el.style.setProperty("--mx", `${((e.clientX - r.left) / r.width) * 100}%`);
    el.style.setProperty("--my", `${((e.clientY - r.top) / r.height) * 100}%`);
  }, []);
}
