import { addImageContain, addNotes, addTitle } from "./common.mjs";

export function renderSequence(slide, page, ctx) {
  addTitle(slide, page.title, { fontFace: ctx.fontFace });
  const steps = page.steps || [];
  const width = 8.8 / Math.max(steps.length, 1);
  steps.forEach((step, index) => {
    const x = 0.6 + index * width;
    const path = ctx.assetPath(step.asset_id);
    if (path) {
      addImageContain(slide, path, { x, y: 1.3, w: width - 0.2, h: 2.6 });
    }
    const label = step.caption || ctx.claimTexts(step.claim_ids)[0] || "";
    slide.addText(`${index + 1}. ${label}`, {
      x,
      y: 4.0,
      w: width - 0.2,
      h: 0.5,
      fontSize: 14,
      fontFace: ctx.fontFace,
    });
  });
  addNotes(slide, page.notes);
}
