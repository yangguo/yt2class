import { addBullets, addNotes, addSourceFooter, addTitle } from "./common.mjs";

export function renderSummary(slide, page, ctx) {
  addTitle(slide, page.title, { fontFace: ctx.fontFace });
  const lines =
    page.bullets?.length === page.claim_ids?.length
      ? page.bullets
      : ctx.claimTexts(page.claim_ids);
  addBullets(slide, lines, { fontFace: ctx.fontFace });
  addNotes(slide, page.notes);
  addSourceFooter(slide, page, ctx);
}
