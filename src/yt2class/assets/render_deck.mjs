import fs from "node:fs/promises";
import path from "node:path";
import { Presentation, PresentationFile } from "@oai/artifact-tool";

const args = process.argv.slice(2);

function getArg(name) {
  const index = args.indexOf(`--${name}`);
  if (index < 0 || !args[index + 1]) {
    throw new Error(`Missing --${name}`);
  }
  return args[index + 1];
}

function color(value) {
  return value || "#000000";
}

function addText(slide, name, value, position, options = {}) {
  const shape = slide.shapes.add({
    geometry: "textbox",
    name,
    position,
    fill: "none",
    line: { style: "solid", fill: "none", width: 0 },
  });
  shape.text = String(value ?? "");
  shape.text.style = {
    fontSize: options.fontSize || 20,
    typeface: "Helvetica Neue",
    color: color(options.color),
    bold: Boolean(options.bold),
    alignment: options.alignment || "left",
    verticalAlignment: options.verticalAlignment || "top",
    autoFit: options.autoFit || "shrinkText",
    wrap: "square",
    insets: options.insets || { top: 0, right: 0, bottom: 0, left: 0 },
  };
  return shape;
}

function addPanel(slide, name, position, fill = "#F2F2F2") {
  return slide.shapes.add({
    geometry: "rect",
    name,
    position,
    fill,
    line: { style: "solid", fill: "#B8BCC4", width: 1 },
  });
}

function addRule(slide, name, left, top, width, fill = "#3D8DFF") {
  return slide.shapes.add({
    geometry: "rect",
    name,
    position: { left, top, width, height: 4 },
    fill,
    line: { style: "solid", fill: "none", width: 0 },
  });
}

async function readImageBlob(imagePath) {
  const bytes = await fs.readFile(imagePath);
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
}

async function addSourceImage(slide, frame, position, name) {
  const imagePath = path.resolve(frame.frame_path);
  const imageBytes = await readImageBlob(imagePath);
  const contentType = imagePath.toLowerCase().endsWith(".png") ? "image/png" : "image/jpeg";
  return slide.images.add({
    blob: imageBytes,
    contentType,
    alt: `Original source frame at ${Number(frame.timestamp).toFixed(3)} seconds`,
    fit: "contain",
    position,
    geometry: "roundRect",
    borderRadius: "rounded-xl",
  });
}

function sourceNotes(spec, frame) {
  const lines = [
    "[Sources]",
    `- Source video: ${spec.source_url || "local source"}`,
  ];
  if (frame) {
    lines.push(`- Original frame: ${frame.frame_path}`);
    lines.push(`- Timestamp: ${Number(frame.timestamp).toFixed(3)}s`);
    if (frame.source_sha256) lines.push(`- Frame SHA-256: ${frame.source_sha256}`);
  }
  return lines.join("\n");
}

function addNotes(slide, notes) {
  slide.speakerNotes.textFrame.setText(notes);
  slide.speakerNotes.setVisible(true);
}

function addPageNumber(slide, number) {
  addText(
    slide,
    `page-${number}`,
    String(number).padStart(2, "0"),
    { left: 1184, top: 659, width: 54, height: 26 },
    { fontSize: 13, alignment: "right", verticalAlignment: "bottom" },
  );
}

async function buildCover(presentation, spec) {
  const slide = presentation.slides.add();
  slide.background.fill = "#FFFFFF";
  addText(slide, "cover-kicker", "YT2CLASS · 原始视频讲义", { left: 42, top: 42, width: 540, height: 30 }, { fontSize: 18, bold: true, color: "#3D8DFF" });
  addText(slide, "cover-title", spec.title, { left: 42, top: 128, width: 560, height: 150 }, { fontSize: 54, bold: true, autoFit: "shrinkText" });
  addRule(slide, "cover-rule", 42, 300, 160);
  addText(slide, "cover-subtitle", spec.subtitle || "原始视频画面讲义", { left: 42, top: 334, width: 520, height: 90 }, { fontSize: 26, color: "#334155" });
  addText(slide, "cover-source", "图片来自视频原始画面，按课程顺序保留。", { left: 42, top: 560, width: 540, height: 50 }, { fontSize: 18, color: "#64748B" });
  const firstFrame = spec.slides[0];
  addPanel(slide, "cover-frame-panel", { left: 658, top: 42, width: 580, height: 588 }, "#EAF5FB");
  await addSourceImage(slide, firstFrame, { left: 674, top: 58, width: 548, height: 556 }, "cover-original-frame");
  addPageNumber(slide, 1);
  addNotes(slide, sourceNotes(spec, firstFrame));
}

async function buildLearningSlide(presentation, spec, frame, index) {
  const slide = presentation.slides.add();
  slide.background.fill = "#FFFFFF";
  addText(slide, `lesson-title-${index}`, frame.title, { left: 42, top: 38, width: 1196, height: 88 }, { fontSize: 38, bold: true });
  addRule(slide, `lesson-rule-${index}`, 42, 136, 120);
  const imageOnLeft = index % 2 === 0;
  const imagePosition = imageOnLeft
    ? { left: 42, top: 172, width: 720, height: 404 }
    : { left: 518, top: 172, width: 720, height: 404 };
  const textPosition = imageOnLeft
    ? { left: 810, top: 182, width: 410, height: 390 }
    : { left: 42, top: 182, width: 430, height: 390 };
  addPanel(slide, `lesson-panel-${index}`, imagePosition, "#F4FAFD");
  await addSourceImage(slide, frame, { left: imagePosition.left + 10, top: imagePosition.top + 10, width: imagePosition.width - 20, height: imagePosition.height - 20 }, `lesson-frame-${index}`);
  addText(slide, `lesson-kind-${index}`, String(frame.kind || "重点").toUpperCase(), { left: textPosition.left, top: textPosition.top, width: textPosition.width, height: 34 }, { fontSize: 17, bold: true, color: "#3D8DFF" });
  addText(slide, `lesson-explanation-${index}`, frame.explanation_zh, { left: textPosition.left, top: textPosition.top + 52, width: textPosition.width, height: 220 }, { fontSize: 23, color: "#0F172A" });
  if (frame.takeaway) {
    addText(slide, `lesson-takeaway-${index}`, `要点：${frame.takeaway}`, { left: textPosition.left, top: textPosition.top + 292, width: textPosition.width, height: 80 }, { fontSize: 19, bold: true, color: "#334155" });
  }
  addText(slide, `lesson-timestamp-${index}`, `原始画面 · ${Number(frame.timestamp).toFixed(1)}s`, { left: 42, top: 650, width: 360, height: 24 }, { fontSize: 15, color: "#64748B" });
  addPageNumber(slide, index + 1);
  addNotes(slide, sourceNotes(spec, frame));
}

function buildSummarySlide(presentation, spec, slideNumber) {
  const slide = presentation.slides.add();
  slide.background.fill = "#FFFFFF";
  addText(slide, "summary-title", "本课总结", { left: 42, top: 38, width: 1196, height: 80 }, { fontSize: 42, bold: true });
  addText(slide, "summary-lead", `围绕「${spec.title}」整理出可回看的原始课程证据。`, { left: 42, top: 132, width: 1196, height: 90 }, { fontSize: 23, color: "#334155" });
  const summaries = (spec.summary || []).slice(0, 3);
  const xPositions = [42, 452, 862];
  summaries.forEach((item, index) => {
    const x = xPositions[index];
    addPanel(slide, `summary-panel-${index}`, { left: x, top: 286, width: 374, height: 324 }, "#F2F2F2");
    addText(slide, `summary-number-${index}`, String(index + 1).padStart(2, "0"), { left: x + 30, top: 330, width: 160, height: 100 }, { fontSize: 56, bold: true, color: "#3D8DFF" });
    addText(slide, `summary-text-${index}`, item, { left: x + 30, top: 472, width: 314, height: 100 }, { fontSize: 22, color: "#0F172A" });
  });
  addPageNumber(slide, slideNumber);
  addNotes(slide, sourceNotes(spec, null));
}

function buildQuizSlide(presentation, spec, slideNumber) {
  const slide = presentation.slides.add();
  slide.background.fill = "#FFFFFF";
  addText(slide, "quiz-kicker", "复习", { left: 42, top: 42, width: 170, height: 34 }, { fontSize: 20, bold: true, color: "#3D8DFF" });
  addText(slide, "quiz-title", "小测验", { left: 42, top: 150, width: 1000, height: 130 }, { fontSize: 64, bold: true });
  const prompts = (spec.quiz || []).map((item, index) => `${index + 1}. ${item.prompt}`).join("\n\n");
  addText(slide, "quiz-prompts", prompts, { left: 42, top: 350, width: 1080, height: 210 }, { fontSize: 28, color: "#0F172A" });
  addText(slide, "quiz-hint", "先独立作答，再回看对应原始画面核对。", { left: 42, top: 610, width: 720, height: 38 }, { fontSize: 18, color: "#64748B" });
  addPageNumber(slide, slideNumber);
  const answerNotes = (spec.quiz || []).map((item, index) => `${index + 1}. ${item.answer}`).join("\n");
  addNotes(slide, `${sourceNotes(spec, null)}\n\n[Answers]\n${answerNotes}`);
}

async function main() {
  const specPath = path.resolve(getArg("spec"));
  const outputPath = path.resolve(getArg("output"));
  const previewDir = args.includes("--preview-dir") ? path.resolve(getArg("preview-dir")) : null;
  const spec = JSON.parse(await fs.readFile(specPath, "utf8"));
  if (!Array.isArray(spec.slides) || spec.slides.length === 0) throw new Error("deck spec has no lesson slides");
  const presentation = Presentation.create({ slideSize: { width: 1280, height: 720 } });
  await buildCover(presentation, spec);
  for (const [index, frame] of spec.slides.entries()) await buildLearningSlide(presentation, spec, frame, index);
  buildSummarySlide(presentation, spec, spec.slides.length + 2);
  buildQuizSlide(presentation, spec, spec.slides.length + 3);
  await fs.mkdir(path.dirname(outputPath), { recursive: true });
  if (previewDir) {
    await fs.mkdir(previewDir, { recursive: true });
    for (const [index, slide] of presentation.slides.items.entries()) {
      const stem = `slide-${String(index + 1).padStart(2, "0")}`;
      const png = await presentation.export({ slide, format: "png", scale: 1 });
      await fs.writeFile(path.join(previewDir, `${stem}.png`), new Uint8Array(await png.arrayBuffer()));
      const layout = await slide.export({ format: "layout" });
      await fs.writeFile(path.join(previewDir, `${stem}.layout.json`), await layout.text(), "utf8");
    }
    const montage = await presentation.export({ format: "webp", montage: true, scale: 1 });
    await fs.writeFile(path.join(previewDir, "deck-montage.webp"), new Uint8Array(await montage.arrayBuffer()));
  }
  const pptx = await PresentationFile.exportPptx(presentation);
  await pptx.save(outputPath);
}

main().catch((error) => {
  console.error(error?.stack || error);
  process.exitCode = 1;
});
