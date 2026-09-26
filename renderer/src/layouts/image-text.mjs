import { addBullets, addImageContain, addNotes, addSourceFooter, addTitle } from "./common.mjs";
import { contentBulletLines } from "./links.mjs";

export function renderImageText(slide, page, ctx) {
  addTitle(slide, page.title, { fontFace: ctx.fontFace });
  const assetId = page.frame_asset_ids?.[0];
  if (assetId) {
    addImageContain(slide, ctx.assetPath(assetId), { x: 0.6, y: 1.3, w: 4.2, h: 3.8 });
  }
  const lines = contentBulletLines(page, ctx.claimTexts(page.point_claim_ids));
  addBullets(slide, lines, { x: 5.0, y: 1.4, w: 4.4 });
  addNotes(slide, page.notes);
  addSourceFooter(slide, page, ctx);
}
