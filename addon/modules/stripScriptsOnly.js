// modules/stripScriptsOnly.js
export async function run(doc, ctx) {
  const scripts = doc.querySelectorAll("script");
  const count = scripts.length;
  scripts.forEach(el => el.remove());   // removes tag + its content
  ctx.stats = ctx.stats || {};
  ctx.stats.removedScripts = count;
  return { doc, ctx };
}
