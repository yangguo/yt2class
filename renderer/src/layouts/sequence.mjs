import { addImageContain, addNotes, addSourceFooter, addTitle } from "./common.mjs";
import { floorYoutubeSeek } from "./links.mjs";

export function renderSequence(slide, page, ctx) {
  addTitle(slide, page.title, { fontFace: ctx.fontFace });
  const steps = page.steps || [];
  const width = 8.8 / Math.max(steps.length, 1);
  steps.forEach((step, index) => {
    const x = 0.6 + index * width;
    const path = ctx.assetPath(step.asset_id);
    const stamp = ctx.assetTimestamp(step.asset_id);
    const link =
      ctx.spec?.source?.kind === "youtube" && ctx.spec?.source?.url
        ? floorYoutubeSeek(ctx.spec.source.url, stamp)
        : null;
    if (path) {
      const imageOpts = { x, y: 1.3, w: width - 0.2, h: 2.6 };
      if (link) imageOpts.hyperlink = { url: link, tooltip: `Step ${index + 1}` };
      addImageContain(slide, path, imageOpts);
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
  addSourceFooter(slide, page, ctx);
}
