import fc from "fast-check";
import { describe, expect, it } from "vitest";
import { formatDuration } from "./duration";

describe("formatDuration", () => {
  it("formats known values as zero-padded HH:MM:SS", () => {
    expect(formatDuration(0)).toBe("00:00:00");
    expect(formatDuration(5)).toBe("00:00:05");
    expect(formatDuration(65)).toBe("00:01:05");
    expect(formatDuration(3661)).toBe("01:01:01");
    expect(formatDuration(360000)).toBe("100:00:00");
  });

  // Feature: meeting-minutes-service, Property: duration formatter (Req 2.2)
  it("round-trips any non-negative integer seconds (>=100 runs)", () => {
    fc.assert(
      fc.property(fc.nat(), (seconds) => {
        const out = formatDuration(seconds);

        // shape: at least two hour digits, exactly two for mm and ss
        expect(out).toMatch(/^\d{2,}:\d{2}:\d{2}$/);

        const [hh, mm, ss] = out.split(":").map((p) => Number(p));
        expect(mm).toBeGreaterThanOrEqual(0);
        expect(mm).toBeLessThanOrEqual(59);
        expect(ss).toBeGreaterThanOrEqual(0);
        expect(ss).toBeLessThanOrEqual(59);

        // parsing back reproduces the input exactly
        expect(hh * 3600 + mm * 60 + ss).toBe(seconds);
      }),
      { numRuns: 200 },
    );
  });
});
