export function floorYoutubeSeek(url, seconds) {
  if (!url) return null;
  const match = url.match(/[?&]v=([A-Za-z0-9_-]{1,64})/);
  const videoId = match?.[1] ?? (url.includes("youtu.be/") ? url.split("youtu.be/")[1]?.split(/[?#]/)[0] : null);
  if (!videoId) return null;
  const stamp = Math.max(0, Math.floor(Number(seconds) || 0));
  return `https://www.youtube.com/watch?v=${videoId}&t=${stamp}s`;
}

export function formatLocalSeek(mediaPath, seconds) {
  const name = mediaPath?.split("/").pop() || "source";
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const mm = String(Math.floor(total / 60)).padStart(2, "0");
  const ss = String(total % 60).padStart(2, "0");
  return `${name} @ ${mm}:${ss}`;
}

export function evidenceStartSeconds(evidenceId, evidenceById) {
  const ev = evidenceById?.[evidenceId];
  if (!ev) return null;
  if (ev.timestamp_seconds != null) return ev.timestamp_seconds;
  if (ev.start_seconds != null) return ev.start_seconds;
  return null;
}

export function primarySeekSeconds(page, assets, evidenceByAsset, evidenceById = {}) {
  const times = [];
  const assetIds = [];
  if (page.type === "content" && page.layout === "sequence") {
    for (const step of page.steps || []) assetIds.push(step.asset_id);
  } else if (page.type === "content" && page.frame_asset_ids?.length) {
    assetIds.push(...page.frame_asset_ids);
  } else if (page.hero_asset_id) {
    assetIds.push(page.hero_asset_id);
  }
  for (const assetId of assetIds) {
    const asset = assets[assetId];
    if (asset?.timestamp_seconds != null) times.push(Number(asset.timestamp_seconds));
    const ev = evidenceByAsset[assetId];
    if (ev?.timestamp_seconds != null) times.push(Number(ev.timestamp_seconds));
  }
  for (const evidenceId of page.citation_ids || []) {
    const stamp = evidenceStartSeconds(evidenceId, evidenceById);
    if (stamp != null) times.push(Number(stamp));
  }
  if (times.length) return Math.min(...times);
  return 0;
}

export function buildSeekLink(
  spec,
  page,
  assets,
  evidenceByAsset,
  evidenceById = {},
  secondsOverride = null,
) {
  const seconds =
    secondsOverride != null
      ? secondsOverride
      : primarySeekSeconds(page, assets, evidenceByAsset, evidenceById);
  if (spec.source?.kind === "youtube" && spec.source.url) {
    const url = floorYoutubeSeek(spec.source.url, seconds);
    if (url) return { url, label: `来源视频 ${Math.floor(seconds)}s` };
  }
  const label = formatLocalSeek(spec.source?.media_path, seconds);
  return { url: null, label };
}
