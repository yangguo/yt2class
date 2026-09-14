import { addBullets, addNotes, addTitle } from "./common.mjs";

export function renderText(slide, page, ctx) {
  addTitle(slide, page.title, { fontFace: ctx.fontFace });
  addBullets(slide, ctx.claimTexts(page.point_claim_ids), { fontFace: ctx.fontFace });
  addNotes(slide, page.notes);
}
