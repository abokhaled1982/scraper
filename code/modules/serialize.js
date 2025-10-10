// modules/serialize.js
// modules/serialize.js
export async function run(doc, ctx) {
  ctx.output = { html: (doc.documentElement || doc).outerHTML };
  return { doc, ctx };
}
