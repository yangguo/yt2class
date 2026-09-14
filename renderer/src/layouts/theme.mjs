export const THEME = {
  name: "course-white-blue",
  accent: "3D8DFF",
  titleColor: "1A1A1A",
  bodyColor: "333333",
  mutedColor: "666666",
  panel: "F2F2F2",
  border: "B8BCC4",
};

export function resolveFont(themeFont, detected) {
  if (detected) return detected;
  return themeFont || "Arial";
}

export function detectCjkFont() {
  if (process.env.YT2CLASS_FORCE_FONT) {
    return process.env.YT2CLASS_FORCE_FONT;
  }
  return null;
}
