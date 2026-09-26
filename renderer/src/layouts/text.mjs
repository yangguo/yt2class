import { addBullets, addNotes, addSourceFooter, addTitle } from "./common.mjs";
import { contentBulletLines } from "./links.mjs";

export function renderText(slide, page, ctx) {
  addTitle(slide, page.title, { fontFace: ctx.fontFace });
  addBullets(slide, contentBulletLines(page, ctx.claimTexts(page.point_claim_ids)), {
    fontFace: ctx.fontFace,
  });
  addNotes(slide, page.notes);
  addSourceFooter(slide, page, ctx);
}
