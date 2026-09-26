import fs from "node:fs/promises";
import path from "node:path";
import PptxGenJS from "pptxgenjs";
import { renderCover } from "./layouts/cover.mjs";
import { renderImageText } from "./layouts/image-text.mjs";
import { renderText } from "./layouts/text.mjs";
import { renderComparison } from "./layouts/comparison.mjs";
import { renderSequence } from "./layouts/sequence.mjs";
import { renderSummary } from "./layouts/summary.mjs";
import { renderQuiz } from "./layouts/quiz.mjs";
import { detectCjkFont, resolveFont } from "./layouts/theme.mjs";
import { buildSeekLink } from "./layouts/links.mjs";

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 1) {
    if (!argv[i].startsWith("--")) continue;
    const key = argv[i].slice(2);
    args[key] = argv[i + 1];
    i += 1;
  }
  return args;
}

function layoutKey(page) {
  if (page.type === "content") return page.layout || "text";
  return page.type;
}

export async function renderSpec(spec, runRoot, outputPath) {
  const pptx = new PptxGenJS();
  pptx.layout = "LAYOUT_16x9";
  pptx.author = "yt2class";
  const fontFace = resolveFont(spec.theme?.font_family, detectCjkFont());
  const claims = Object.fromEntries((spec.claims || []).map((c) => [c.id, c.text]));
  const assets = Object.fromEntries((spec.assets || []).map((a) => [a.id, a]));
  const frameEvidence = {};
  const evidenceById = {};
  for (const item of spec.evidence || []) {
    evidenceById[item.id] = item;
    if (item.kind === "frame") frameEvidence[item.asset_id] = item;
  }
  const ctx = {
    spec,
    fontFace,
    claimTexts(ids) {
      return (ids || []).map((id) => claims[id] || id);
    },
    assetPath(assetId) {
      const asset = assets[assetId];
      if (!asset) return null;
      return path.resolve(runRoot, asset.path);
    },
    assetTimestamp(assetId) {
      const asset = assets[assetId];
      if (asset?.timestamp_seconds != null) return asset.timestamp_seconds;
      const ev = frameEvidence[assetId];
      return ev?.timestamp_seconds ?? 0;
    },
    seekLinkForPage(page) {
      return buildSeekLink(
        spec,
        page,
        assets,
        frameEvidence,
        evidenceById,
      );
    },
  };
  const pageMap = [];
  for (const [index, page] of (spec.slides || []).entries()) {
    const slide = pptx.addSlide();
    const key = layoutKey(page);
    switch (key) {
      case "cover":
        renderCover(slide, page, ctx);
        break;
      case "image-text":
        renderImageText(slide, page, ctx);
        break;
      case "text":
        renderText(slide, page, ctx);
        break;
      case "comparison":
        renderComparison(slide, page, ctx);
        break;
      case "sequence":
        renderSequence(slide, page, ctx);
        break;
      case "summary":
        renderSummary(slide, page, ctx);
        break;
      case "quiz":
        renderQuiz(slide, page, ctx);
        break;
      default:
        renderText(slide, page, ctx);
    }
    pageMap.push({ page_id: page.id, pptx_slide_index: index, layout: key });
  }
  await pptx.writeFile({ fileName: outputPath });
  return { page_map: pageMap, slide_count: pageMap.length };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const specPath = path.resolve(args.spec);
  const runRoot = path.resolve(args["run-root"]);
  const outputPath = path.resolve(args.output);
  const reportPath = args["report-path"] ? path.resolve(args["report-path"]) : null;
  const spec = JSON.parse(await fs.readFile(specPath, "utf8"));
  const result = await renderSpec(spec, runRoot, outputPath);
  if (reportPath) {
    await fs.writeFile(reportPath, JSON.stringify(result, null, 2), "utf8");
  }
}

import { fileURLToPath } from "node:url";

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    console.error(error);
    process.exit(1);
  });
}
