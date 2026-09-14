import { addImageContain, addNotes, addTitle } from "./common.mjs";

export function renderComparison(slide, page, ctx) {
  addTitle(slide, page.title, { fontFace: ctx.fontFace });
  const frames = page.frame_asset_ids || [];
  const captions = page.captions || [];
  const boxes = [
    { x: 0.5, y: 1.2, w: 4.3, h: 3.2 },
    { x: 5.2, y: 1.2, w: 4.3, h: 3.2 },
  ];
  frames.slice(0, 2).forEach((assetId, index) => {
    const path = ctx.assetPath(assetId);
    if (path) addImageContain(slide, path, boxes[index]);
    const caption = captions[index] || "";
    if (caption) {
      slide.addText(caption, {
        x: boxes[index].x,
        y: 4.5,
        w: boxes[index].w,
        h: 0.4,
        fontSize: 14,
        fontFace: ctx.fontFace,
      });
    }
  });
  addNotes(slide, page.notes);
}
