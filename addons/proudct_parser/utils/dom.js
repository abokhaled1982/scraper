// utils/dom.js
export function makeDetachedDocumentFromPage() {
  const html = document.documentElement.outerHTML;
  return new DOMParser().parseFromString(html, "text/html");
}
