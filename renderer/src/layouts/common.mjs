import { readFileSync } from "node:fs";
import { imageSize } from "image-size";
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

export function addImageContain(slide, imagePath, box, extra = {}) {
  const { width: sourceWidth, height: sourceHeight } = imageSize(
    readFileSync(imagePath),
  );
  if (!sourceWidth || !sourceHeight) {
    throw new Error(`cannot determine image dimensions: ${imagePath}`);
  }
  const scale = Math.min(box.w / sourceWidth, box.h / sourceHeight);
  const width = sourceWidth * scale;
  const height = sourceHeight * scale;
  slide.addImage({
    path: imagePath,
    ...extra,
    x: box.x + (box.w - width) / 2,
    y: box.y + (box.h - height) / 2,
    w: width,
    h: height,
  });
}

export function addNotes(slide, text) {
  if (text) {
    slide.addNotes(text);
  }
}

export function addSourceFooter(slide, page, ctx) {
  const link = ctx.seekLinkForPage(page);
  if (!link) return;
  const options = {
    x: 0.6,
    y: 5.15,
    w: 8.8,
    h: 0.35,
    fontSize: 10,
    color: "666666",
    fontFace: ctx.fontFace,
  };
  if (link.url) {
    options.hyperlink = { url: link.url, tooltip: link.label };
  }
  slide.addText(link.label, options);
}
