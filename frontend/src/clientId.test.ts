import { afterEach, describe, expect, it, vi } from "vitest";
import { createClientId } from "./clientId";

describe("createClientId", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("uses randomUUID when the browser provides it", () => {
    vi.stubGlobal("crypto", { randomUUID: () => "native-id" });

    expect(createClientId()).toBe("native-id");
  });

  it("generates a UUID when randomUUID is unavailable on HTTP origins", () => {
    vi.stubGlobal("crypto", {
      getRandomValues: (bytes: Uint8Array) => {
        bytes.set(Array.from({ length: 16 }, (_, index) => index));
        return bytes;
      },
    });

    expect(createClientId()).toBe("00010203-0405-4607-8809-0a0b0c0d0e0f");
  });

  it("still returns a unique-shaped key without Web Crypto", () => {
    vi.stubGlobal("crypto", undefined);
    vi.spyOn(Date, "now").mockReturnValue(1_700_000_000_000);
    vi.spyOn(Math, "random").mockReturnValue(0.5);

    expect(createClientId()).toMatch(/^client-[a-z0-9]+-[a-z0-9]+$/);
  });
});
