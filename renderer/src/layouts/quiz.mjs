import { addNotes, addTitle } from "./common.mjs";

export function renderQuiz(slide, page, ctx) {
  addTitle(slide, page.title, { fontFace: ctx.fontFace });
  const lines = (page.questions || []).map((q, i) => `${i + 1}. ${q.prompt}`);
  slide.addText(
    lines.map((line) => ({ text: line, options: { bullet: true } })),
    {
      x: 0.6,
      y: 1.4,
      w: 8.8,
      h: 3.5,
      fontSize: 18,
      fontFace: ctx.fontFace,
    },
  );
  const answers = (page.questions || [])
    .map((q) => ctx.claimTexts(q.answer_claim_ids).join(" / "))
    .filter(Boolean)
    .join("\n");
  addNotes(slide, [page.notes, answers ? `Answers:\n${answers}` : ""].filter(Boolean).join("\n\n"));
}
