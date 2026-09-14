import { addImageContain, addNotes, addSourceFooter, addTitle } from "./common.mjs";

export function renderCover(slide, page, ctx) {
  addTitle(slide, page.title, { fontFace: ctx.fontFace, y: 1.2, fontSize: 36 });
  if (page.subtitle) {
    slide.addText(page.subtitle, {
      x: 0.6,
      y: 2.0,
      w: 8.8,
      h: 0.6,
      fontSize: 20,
      color: "666666",
      fontFace: ctx.fontFace,
    });
  }
  if (page.hero_asset_id && ctx.assetPath(page.hero_asset_id)) {
    addImageContain(slide, ctx.assetPath(page.hero_asset_id), {
      x: 6.5,
      y: 0.5,
      w: 3.0,
      h: 2.5,
    });
  }
  addNotes(slide, page.notes);
  addSourceFooter(slide, page, ctx);
}
