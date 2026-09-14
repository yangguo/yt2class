import { THEME } from "./theme.mjs";

export const SLIDE = { w: 10, h: 5.625 };

export function addTitle(slide, text, opts = {}) {
  slide.addText(text, {
    x: 0.6,
    y: opts.y ?? 0.35,
    w: 8.8,
    h: opts.h ?? 0.8,
    fontSize: opts.fontSize ?? 28,
    bold: true,
    color: THEME.titleColor,
    fontFace: opts.fontFace,
    valign: "top",
  });
}

export function addBullets(slide, lines, opts = {}) {
  const body = lines.map((line) => ({ text: line, options: { bullet: true } }));
  slide.addText(body, {
    x: opts.x ?? 0.6,
    y: opts.y ?? 1.4,
    w: opts.w ?? 8.8,
    h: opts.h ?? 3.5,
    fontSize: opts.fontSize ?? 18,
    color: THEME.bodyColor,
    fontFace: opts.fontFace,
    valign: "top",
  });
}

export function addImageContain(slide, imagePath, box) {
  slide.addImage({
    path: imagePath,
    x: box.x,
    y: box.y,
    w: box.w,
    h: box.h,
    sizing: { type: "contain", w: box.w, h: box.h },
  });
}

export function addNotes(slide, text) {
  if (text) {
    slide.addNotes(text);
  }
}
