// modules/extractReadable.js
// Baut eine einfache "Readable"-Struktur (Titel, Absätze, Überschriften, Links)
export async function run(doc, ctx) {
  const title = (doc.querySelector("title")?.textContent || "").trim();
  const metaDesc = doc.querySelector('meta[name="description"]')?.content || "";

  const blocks = [];
  doc.querySelectorAll("h1,h2,h3,h4,p,li").forEach(el => {
    const txt = (el.textContent || "").trim();
    if (txt) blocks.push({ tag: el.tagName.toLowerCase(), text: txt });
  });

  const links = Array.from(doc.querySelectorAll("a[href]"))
    .map(a => ({ text: (a.textContent || "").trim(), href: a.href }))
    .filter(x => x.href && /^https?:/i.test(x.href));

  ctx.readable = { title, description: metaDesc, blocks, links };
  return { doc, ctx };
}
