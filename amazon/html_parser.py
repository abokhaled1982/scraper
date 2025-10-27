# html_parser.py

from __future__ import annotations
import re
from typing import Dict, Any, List
from bs4 import BeautifulSoup, Comment
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode 

# ----------------------------- URL Normalisierung & Extraktion --------------------------------------

def normalize_url(url: str) -> str:
    """
    Normalisiert eine URL, indem irrelevante Query-Parameter und Fragmente entfernt werden.
    """
    irrelevant_params = {
        'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
        'ref', 'aff_id', 'trkid', 'fbclid', 'gclid', 'msclkid', 'icid', 
        'source', 'medium', 'campaign', 'pbraid', 'cid', 'sp_rid', 'cmpid', 
        'partner', 'session_id'
    }

    parsed_url = urlparse(url)
    url_without_fragment = parsed_url._replace(fragment='')
    query_params = parse_qs(url_without_fragment.query)
    
    cleaned_params = {}
    for key, value in query_params.items():
        if key.lower() not in irrelevant_params:
            cleaned_params[key] = value
            
    cleaned_query = urlencode(cleaned_params, doseq=True)
    normalized_url = url_without_fragment._replace(query=cleaned_query)
    
    return urlunparse(normalized_url._replace(
        scheme=normalized_url.scheme.lower(),
        netloc=normalized_url.netloc.lower()
    ))

def extract_and_normalize_url(html_content: str) -> str:
    """
    Extrahiert die reinste Produkt-URL aus dem HTML mithilfe intelligenter
    Suche und normalisiert sie anschließend.
    """
    found_url = None
    
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 1. HÖCHSTE PRIORITÄT: Kanonische Links
        canonical_link = soup.find('link', {'rel': 'canonical'})
        if canonical_link and canonical_link.get('href'):
            found_url = canonical_link['href'].strip()
        
        # 2. ZWEITE PRIORITÄT: Open Graph Tags
        if not found_url:
            og_url_meta = soup.find('meta', {'property': 'og:url'})
            if og_url_meta and og_url_meta.get('content'):
                found_url = og_url_meta['content'].strip()

        # 3. DRITTE PRIORITÄT: Apple iTunes/App Meta-Tag (z.B. für App-Stores)
        if not found_url:
            apple_meta = soup.find('meta', {'name': 'apple-itunes-app'})
            if apple_meta and apple_meta.get('content'):
                content = apple_meta['content']
                match = re.search(r'app-argument=(https?://.+)', content)
                if match:
                    found_url = match.group(1).strip()
        
        # 4. VIERTE PRIORITÄT: Schema.org Product/Offers URL
        if not found_url:
            product_links = soup.select('[itemtype*="schema.org/Product"] a[href], [itemtype*="schema.org/Offer"] a[href]')
            if product_links:
                found_url = max([link['href'] for link in product_links if link.get('href')], key=len, default=None)

        # 5. LETZTER FALLBACK: AGGRESSIVE REGEX-SUCHE im gesamten HTML-Text
        if not found_url:
             # Sucht nach http/https URLs, die keine statischen Ressourcen (Bilder, JS, CSS) sind
             # Wir suchen URLs mit mindestens 3 Pfadsegmenten (wahrscheinlich spezifische Produkt-URL)
             urls = re.findall(r'https?://(?:www\.)?(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(?:/[^\s"\']*)?', html_content)
             
             # Filtert nach URLs, die spezifischer erscheinen (mind. 3 Pfadsegmente und keine zu kurzen URLs)
             product_urls = [u for u in urls if u.count('/') >= 3 and len(u) > 30 and not any(ext in u for ext in ['.js', '.css', '.png', '.jpg', '.svg'])] 
             
             if product_urls:
                 # Wählt die längste gefundene URL, da sie am spezifischsten ist
                 found_url = max(product_urls, key=len)

        # 6. Normalisierung der URL
        if found_url and found_url.startswith('http'):
            return normalize_url(found_url)
                
    except Exception as e:
        print(f"WARNUNG: Fehler beim Extrahieren oder Normalisieren der URL: {e}", file=sys.stderr)
        
    return "N/A"

def extract_title_from_html(html_content: str) -> str:
    """Extrahiert den bereinigten Titel."""
    soup = BeautifulSoup(html_content, 'lxml')
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
        title = re.sub(r' \| .*| - .*| – .*| :: .*', '', title)
        return title
    
    if h1 := soup.find('h1'):
        return h1.get_text().strip()
    
    if og_title := soup.find('meta', {'property': 'og:title'}):
        if og_title.get('content'):
            return og_title['content'].strip()
            
    return "N/A"


def extract_core_html_data(html_content: str) -> Dict[str, Any]:
    """
    Führt alle deterministischen HTML-Extraktionen durch und konsolidiert sie in einem Dictionary.
    """
    title = extract_title_from_html(html_content)
    url = extract_and_normalize_url(html_content) # <- Ihre Power-Funktion wird hier verwendet
  
    
    return {
        "title_html": title,
        "affiliate_url": url      
    }

def clean_html_to_core_text(html_content: str) -> str:
    """Parst den HTML-Inhalt und entfernt Boilerplate für die LLM-Verarbeitung."""
    soup = BeautifulSoup(html_content, 'lxml')

    ignore_tags = [
        'script', 'style', 'header', 'footer', 'nav', 'iframe', 
        'noscript', 'button', 'link', 'meta', 'svg', 'img', 'picture', 'source',
        'input', 'textarea'
    ]
    
    boilerplate_selectors = [
        '.cookie-banner', '#cookie-consent', '.gdpr-popup', '#site-footer', 
        '#site-header', '.site-nav', '.related-products', '.upsell', '.cross-sell', 
        '.newsletter-signup', '.social-links', 'dialog', 'modal', 'popup', 'menu', 
        'search', 'toolbar', 'banner', 'footer', 'header', '.top-bar', '#utility-nav' 
    ]
    
    for tag in soup(ignore_tags):
        tag.decompose()

    for selector in boilerplate_selectors:
        for element in soup.select(selector):
            element.decompose()

    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()
        
    text = soup.get_text()

    text = re.sub(r'[\t\r\n]+', '\n', text)
    text = re.sub(r' +', ' ', text)
    text = '\n'.join(line.strip() for line in text.split('\n'))
    text = re.sub(r'\n\s*\n', '\n', text).strip()
    
    return text