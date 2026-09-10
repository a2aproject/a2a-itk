import { useEffect, useState } from "react";

type Theme = "auto" | "light" | "dark";

const NEXT: Record<Theme, Theme> = { auto: "light", light: "dark", dark: "auto" };
const LABEL: Record<Theme, string> = { auto: "Auto", light: "Light", dark: "Dark" };
const STORAGE_KEY = "itk-theme";

/**
 * Cycles auto -> light -> dark. `auto` leaves the choice to the OS via
 * `prefers-color-scheme`; the other two stamp `data-theme` on <html>, which
 * index.html also reads before first paint to avoid a flash.
 */
export default function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(
    () => (localStorage.getItem(STORAGE_KEY) as Theme | null) ?? "auto",
  );

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem(STORAGE_KEY, theme);
  }, [theme]);

  const next = NEXT[theme];
  return (
    <button
      type="button"
      className="theme-toggle"
      onClick={() => setTheme(next)}
      aria-label={`Colour theme: ${LABEL[theme]}. Switch to ${LABEL[next]}.`}
    >
      {LABEL[theme]}
    </button>
  );
}
