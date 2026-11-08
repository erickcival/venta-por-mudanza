#!/usr/bin/env python3
"""Genera el sitio (sitio/) y el catálogo PDF a partir de articulos.json + carpetas de fotos.

Uso:  .venv/bin/python build.py            (genera sitio y PDF)
      .venv/bin/python build.py --sin-pdf  (solo sitio)

      python tools/build.py --ci .        (en GitHub Actions: solo regenera el PDF desde
                                          catalogo.json + estado.json, sin fotos originales)

Cada carpeta con fotos es un artículo. Si aparece una carpeta nueva que no está en
articulos.json, se agrega sola con datos mínimos y se avisa en la salida.

El estado de cada artículo (disponible/apartado/vendido/pausado) vive en sitio/estado.json,
que edita el centro de control (sitio/control/). Nunca se guarda en articulos.json.
"""
import hashlib, html, json, os, re, shutil, subprocess, sys, unicodedata, urllib.parse
from pathlib import Path

import qrcode
import qrcode.image.svg
from PIL import Image, ImageOps

RAIZ = Path(__file__).resolve().parent
SITIO = RAIZ / "sitio"
IMG = SITIO / "img"
EXT = {".heic", ".jpg", ".jpeg", ".png", ".webp"}
CHROME = os.environ.get("CHROME") or shutil.which("google-chrome") or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
ESTADOS = ("disponible", "apartado", "vendido", "pausado")
CAT_ORDEN = ["Sala", "Comedor", "Recámara", "Electrónica", "Electrodomésticos", "Cocina", "Oficina", "Terraza", "Bodega", "Deporte", "Otros"]

esc = lambda s: html.escape(str(s or ""), quote=True)


def slugify(s):
    s = re.sub(r"^\d+\.\s*", "", s)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def dinero(n, moneda):
    return f"${n:,.0f} {moneda}" if n is not None else None


# ---------- datos ----------
def cargar():
    data = json.loads((RAIZ / "articulos.json").read_text())
    cfg, ocultar = data["config"], set(data.get("ocultar_fotos", []))
    items = data["articulos"]
    avisos = []

    usadas = set()
    conocidas = {a["carpeta"] for a in items if a.get("carpeta")}
    # cualquier carpeta (incluso dentro de otra) con fotos visibles es un artículo
    for d in sorted(p for p in RAIZ.rglob("*") if p.is_dir()):
        rel = d.relative_to(RAIZ).as_posix()
        if rel.split("/")[0] in ("sitio", ".venv") or any(s.startswith(".") for s in rel.split("/")):
            continue
        if rel in conocidas:
            continue
        fotos = [f for f in d.iterdir() if f.suffix.lower() in EXT and f.stem not in ocultar]
        if fotos:
            items.append({"carpeta": rel, "titulo": re.sub(r"^\d+\.\s*", "", d.name),
                          "categoria": "Otros", "precio": None,
                          "descripcion": "", "detalles": []})
            avisos.append(f"NUEVO: carpeta '{rel}' agregada sin ficha (falta descripción/precio)")

    for a in items:
        a.setdefault("slug", slugify(a["carpeta"]) if a.get("carpeta") else slugify(a["titulo"]))
        src = []
        if a.get("carpeta") and (RAIZ / a["carpeta"]).is_dir():
            src += sorted(f for f in (RAIZ / a["carpeta"]).iterdir()
                          if f.suffix.lower() in EXT and f.stem not in ocultar)
        for ref in a.get("fotos", []):
            hits = [p for p in (RAIZ / ref).parent.glob(Path(ref).name + ".*") if p.suffix.lower() in EXT]
            src += hits
            if not hits:
                avisos.append(f"FALTA FOTO: {ref} ({a['titulo']})")
        a["_src"] = src
        usadas.update(src)
        if not src:
            avisos.append(f"SIN FOTO: {a['titulo']}")
        if a.get("precio") is None:
            avisos.append(f"SIN PRECIO: {a['titulo']}")

    for f in RAIZ.iterdir():
        if f.is_file() and f.suffix.lower() in EXT and f not in usadas and f.stem not in ocultar:
            avisos.append(f"FOTO SUELTA sin artículo: {f.name}")

    return cfg, items, avisos


def leer_hoja(cfg):
    """Lee la pestaña pública de la hoja de control (Google Sheets publicada como CSV).
    Devuelve {slug: (estado, precio)} o None si no hay hoja o no responde."""
    url = cfg.get("hoja_csv")
    if not url:
        return None
    import csv, io, urllib.request
    try:
        with urllib.request.urlopen(url + ("&" if "?" in url else "?") + "t=" + str(os.getpid()), timeout=20) as r:
            texto = r.read().decode("utf-8")
    except Exception as e:
        print("  - AVISO: no pude leer la hoja de control:", e)
        return None
    out = {}
    for fila in csv.DictReader(io.StringIO(texto)):
        slug = (fila.get("id") or "").strip()
        if not slug or slug.startswith("#"):
            continue
        e = (fila.get("estado") or "").strip().lower()
        e = {"pausa": "pausado", "": "disponible"}.get(e, e)
        try:
            precio = float(str(fila.get("precio") or "").replace("$", "").replace(",", "")) or None
        except ValueError:
            precio = None
        out[slug] = (e, precio)
    return out


def aplicar_hoja(items, est, hoja):
    if not hoja:
        return
    est["estados"] = {k: e for k, (e, _) in hoja.items() if e != "disponible"}
    for a in items:
        if a["slug"] in hoja and hoja[a["slug"]][1]:
            a["precio"] = hoja[a["slug"]][1]


def leer_precios(sitio):
    """sitio/precios.json: precios cambiados desde el centro de control ({slug: precio}). Ganan sobre articulos.json."""
    f = sitio / "precios.json"
    return json.loads(f.read_text()) if f.exists() else {}


def aplicar_precios(items, precios):
    for a in items:
        if precios.get(a["slug"]):
            a["precio"] = precios[a["slug"]]


def leer_estado(sitio):
    f = sitio / "estado.json"
    return json.loads(f.read_text()) if f.exists() else {"estados": {}}


def aplicar_estado(items, est):
    for a in items:
        e = est["estados"].get(a["slug"], "disponible")
        a["estado"] = e if e in ESTADOS else "disponible"
    items.sort(key=lambda a: (CAT_ORDEN.index(a["categoria"]) if a["categoria"] in CAT_ORDEN else 99,
                              {"vendido": 2, "apartado": 1}.get(a["estado"], 0)))


# ---------- imágenes ----------
def procesar_fotos(items):
    IMG.mkdir(parents=True, exist_ok=True)
    vivos = set()
    for a in items:
        a["_img"] = []
        for i, src in enumerate(a["_src"], 1):
            nombre = f"{a['slug']}-{i}"
            grande, chica = IMG / f"{nombre}.jpg", IMG / f"{nombre}-t.jpg"
            vivos |= {grande.name, chica.name}
            if not grande.exists() or grande.stat().st_mtime < src.stat().st_mtime:
                tmp = IMG / f"_{nombre}.jpg"
                subprocess.run(["sips", "-s", "format", "jpeg", "-Z", "1600", str(src), "--out", str(tmp)],
                               check=True, capture_output=True)
                im = ImageOps.exif_transpose(Image.open(tmp)).convert("RGB")  # endereza y quita EXIF/GPS
                tmp.unlink()
                im.thumbnail((1400, 1400))
                im.save(grande, "JPEG", quality=74, optimize=True, progressive=True)
                im.thumbnail((640, 640))
                im.save(chica, "JPEG", quality=72, optimize=True, progressive=True)
            a["_img"].append(nombre)
    for f in IMG.iterdir():
        if f.name not in vivos:
            f.unlink()


def qr_svg(url):
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=1)
    s = img.to_string(encoding="unicode")
    return re.sub(r"<\?xml[^>]*\?>", "", s)


# ---------- HTML ----------
CSS = """
:root{--bg:#f6f1ea;--card:#fffdf9;--ink:#1f1b16;--muted:#6f655a;--line:#e4dacd;--accent:#b4532a;--accent-ink:#fff;--ok:#1f7a4d;--wa:#1f8f4e;--sold:#8b2c1d}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#17140f;--card:#211d17;--ink:#f3ece2;--muted:#b3a796;--line:#3a332a;--accent:#e07a4b;--accent-ink:#1a120c;--wa:#2fae66;--sold:#e0705b}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
a{color:inherit}img{display:block;max-width:100%}
.wrap{max-width:1120px;margin:0 auto;padding:0 16px}
h1,h2,.serif{font-family:"Fraunces",Georgia,"Times New Roman",serif;font-weight:600;letter-spacing:-.01em}
header.hero{padding:40px 0 20px}
.hero h1{font-size:clamp(34px,7vw,60px);line-height:1.02;margin:0 0 10px}
.hero p{margin:0;color:var(--muted);max-width:60ch}
.meta{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}
.pill{border:1px solid var(--line);background:var(--card);border-radius:999px;padding:6px 12px;font-size:14px}
.filtros{position:sticky;top:0;z-index:5;background:var(--bg);padding:12px 0;display:flex;gap:8px;overflow-x:auto;scrollbar-width:none;border-bottom:1px solid var(--line)}
.filtros button{flex:none;font:inherit;font-size:14px;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:999px;padding:7px 14px;cursor:pointer}
.filtros button[aria-pressed=true]{background:var(--ink);color:var(--bg);border-color:var(--ink)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:16px;padding:20px 0 40px}
footer{padding-bottom:96px!important}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden;display:flex;flex-direction:column;position:relative}
.card a.foto{aspect-ratio:4/3;background:var(--line);display:block;overflow:hidden}
.card a.foto img{width:100%;height:100%;object-fit:cover}
.card .body{padding:12px 14px 14px;display:flex;flex-direction:column;gap:6px;flex:1}
.card h2{font-size:19px;line-height:1.2;margin:0}
.card h2 a{text-decoration:none}
.precio{font-weight:700;font-size:18px}.precio.consultar{font-weight:500;color:var(--muted);font-size:15px}
.nuevo{color:var(--muted);font-size:13px}
.cat{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;text-decoration:none;font-weight:600;border-radius:10px;padding:11px 14px;background:var(--wa);color:#fff;border:0;font-size:15px;margin-top:auto}
.btn[aria-disabled=true]{background:var(--line);color:var(--muted);pointer-events:none}
.sello{position:absolute;top:12px;left:12px;background:var(--sold);color:#fff;font-weight:800;letter-spacing:.1em;font-size:13px;padding:5px 10px;border-radius:6px;transform:rotate(-4deg)}
.sello.apartado{background:#b8860b}
.card.vendido a.foto img{filter:grayscale(1) opacity(.6)}
[hidden]{display:none!important}
.aviso{background:var(--line);border-radius:10px;padding:10px 12px;font-weight:600;margin:0 0 10px}
.sinfoto{display:grid;place-items:center;height:100%;color:var(--muted);font-size:14px}
.ocultar-vendidos .card.vendido{display:none}
label.toggle{display:flex;align-items:center;gap:6px;font-size:14px;color:var(--muted);flex:none;padding-left:4px}
footer{border-top:1px solid var(--line);padding:24px 0 40px;color:var(--muted);font-size:14px}
.fab{position:fixed;right:16px;bottom:calc(16px + env(safe-area-inset-bottom));z-index:10;display:inline-flex;align-items:center;gap:8px;background:var(--wa);color:#fff;text-decoration:none;font-weight:700;padding:14px 18px;border-radius:999px;box-shadow:0 6px 20px rgba(0,0,0,.25)}
.fab svg{width:22px;height:22px}
.acciones{display:grid;grid-template-columns:1fr auto;gap:8px;margin-top:auto}.acciones .btn{margin:0}
.btn2{font:inherit;font-size:14px;font-weight:600;border:1.5px solid var(--ink);background:transparent;color:var(--ink);border-radius:10px;padding:9px 12px;cursor:pointer;white-space:nowrap}
.btn2[aria-pressed=true]{background:var(--ink);color:var(--bg)}
.carro{position:fixed;z-index:20;font:inherit;font-weight:700;border:0;border-radius:999px;padding:13px 18px;background:var(--accent);color:var(--accent-ink);box-shadow:0 6px 20px rgba(0,0,0,.25);cursor:pointer;display:flex;gap:8px;align-items:center}
.carro[hidden]{display:none}
.carro.abajo{left:16px;right:16px;bottom:calc(16px + env(safe-area-inset-bottom));justify-content:center}
.carro.arriba{top:12px;right:12px;padding:9px 14px;font-size:14px}
@media(min-width:700px){.carro.abajo{left:auto;right:16px}}
dialog.dlg{border:0;border-radius:16px;padding:0;width:min(520px,calc(100% - 24px));max-height:85vh;background:var(--card);color:var(--ink)}
dialog.dlg::backdrop{background:rgba(0,0,0,.5)}
.dlg header{display:flex;justify-content:space-between;align-items:center;padding:16px 18px;border-bottom:1px solid var(--line)}
.dlg header h2{margin:0;font-size:22px}.dlg .x{font:inherit;font-size:22px;background:none;border:0;color:var(--muted);cursor:pointer}
.dlg ul{list-style:none;margin:0;padding:6px 18px;overflow:auto;max-height:50vh}
.dlg li{display:grid;grid-template-columns:52px 1fr auto;gap:10px;align-items:center;padding:10px 0;border-bottom:1px solid var(--line)}
.dlg li img,.dlg li .ph{width:52px;height:52px;border-radius:8px;object-fit:cover;background:var(--line)}
.dlg li .t{font-size:14px;line-height:1.25}.dlg li .t b{display:block;font-size:15px;margin-top:2px}
.dlg .ctl{display:flex;flex-direction:column;align-items:flex-end;gap:4px}
.dlg .qty{display:flex;align-items:center;border:1px solid var(--line);border-radius:8px}
.dlg .qty button{font:inherit;width:30px;height:30px;border:0;background:none;color:var(--ink);cursor:pointer}
.dlg .qty span{min-width:20px;text-align:center;font-weight:600}
.dlg .quitar{font:inherit;font-size:12px;border:0;background:none;color:var(--muted);text-decoration:underline;cursor:pointer}
.dlg footer{padding:14px 18px 18px;border-top:1px solid var(--line)}
.dlg .tot{display:flex;justify-content:space-between;font-size:18px;font-weight:700;margin-bottom:4px}
.dlg .nota{font-size:13px;color:var(--muted);margin:0 0 10px}
.dlg .btn{width:100%}
.dlg .vacio{padding:30px 18px;text-align:center;color:var(--muted)}
.barra .acciones{flex:1;margin:0}.barra .bp{flex:none}.barra .btn,.barra .btn2{white-space:nowrap;padding:11px 10px}.barra .precio{font-size:16px}.barra .nuevo{display:none}
body.con-carro .fab{display:none}
.pdfbtn{display:inline-flex;align-items:center;gap:6px;margin-top:14px;font-weight:600;font-size:15px;text-decoration:none;border:1.5px solid var(--ink);border-radius:10px;padding:9px 14px}
/* artículo */
.volver{display:inline-block;padding:16px 0;text-decoration:none;color:var(--muted)}
.art{display:grid;gap:24px;padding-bottom:110px}
@media(min-width:860px){.art{grid-template-columns:1.3fr 1fr;align-items:start}.panel{position:sticky;top:16px}}
.galeria{display:flex;gap:8px;overflow-x:auto;scroll-snap-type:x mandatory;border-radius:14px;scrollbar-width:thin}
.galeria img{flex:none;width:100%;aspect-ratio:4/3;object-fit:cover;scroll-snap-align:start;border-radius:14px;background:var(--line)}
.galeria.una img{width:100%}
.contador{font-size:13px;color:var(--muted);margin-top:6px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px}
.panel h1{font-size:clamp(26px,4vw,34px);line-height:1.1;margin:4px 0 10px}
.panel ul{padding-left:18px;margin:12px 0}
.panel .btn{width:100%;margin-top:12px}
.barra{position:fixed;left:0;right:0;bottom:0;padding:12px 16px calc(12px + env(safe-area-inset-bottom));background:var(--bg);border-top:1px solid var(--line);display:flex;gap:12px;align-items:center}
.barra .btn{flex:1;margin:0}
@media(min-width:860px){.barra{display:none}}
"""

FONTS = '<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600&display=swap" rel="stylesheet">'
WA_ICON = '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 2a10 10 0 0 0-8.6 15.1L2 22l5-1.3A10 10 0 1 0 12 2Zm5.4 14.1c-.2.6-1.3 1.2-1.8 1.3-.5 0-1 .2-3.3-.7-2.8-1.1-4.6-4-4.7-4.2-.1-.2-1.1-1.5-1.1-2.9s.7-2.1 1-2.4c.2-.3.5-.3.7-.3h.5c.2 0 .4 0 .6.5l.8 2c.1.2.1.4 0 .5l-.4.6-.3.4c-.1.1-.3.3-.1.6.2.3.8 1.3 1.7 2.1 1.2 1 2.1 1.3 2.4 1.5.3.1.5.1.6-.1l.9-1c.2-.3.4-.2.6-.1l1.9.9c.3.1.5.2.5.3.1.2.1.8-.1 1.3Z"/></svg>'


def wa_link(cfg, a):
    num = re.sub(r"\D", "", cfg.get("whatsapp", ""))
    if not num:
        return None
    p = dinero(a.get("precio"), cfg["moneda"])
    txt = f"Hola, me interesa: {a['titulo']}" + (f" ({p})" if p else "") + f"\n{cfg['url']}a/{a['slug']}/"
    return f"https://wa.me/{num}?text={urllib.parse.quote(txt)}"


def wa_general(cfg):
    num = re.sub(r"\D", "", cfg.get("whatsapp", ""))
    txt = f"Hola, vi tu venta por mudanza y me interesa algo: {cfg['url']}"
    return f"https://wa.me/{num}?text={urllib.parse.quote(txt)}" if num else None


def wa_bonito(cfg):
    num = re.sub(r"\D", "", cfg.get("whatsapp", ""))
    return f"{num[2:4]} {num[4:8]} {num[8:]}" if num.startswith("52") and len(num) == 12 else cfg.get("whatsapp", "")


def boton(cfg, a):
    if a.get("estado") in ("vendido", "pausado"):
        return f'<span class="btn" aria-disabled="true">{"Vendido" if a["estado"] == "vendido" else "No disponible"}</span>'
    href = wa_link(cfg, a)
    if not href:
        return '<span class="btn" aria-disabled="true">Contacto próximamente</span>'
    return f'<a class="btn" href="{esc(href)}" target="_blank" rel="noopener">{WA_ICON} Lo quiero</a>'


def agregar(a):
    oculto = " hidden" if a.get("estado") in ("vendido", "pausado") else ""
    return f'<button type="button" class="btn2" data-add="{esc(a["slug"])}"{oculto}>🛒 Agregar</button>'


def acciones(cfg, a):
    # data-wa guarda el link para que carrito.js pueda reactivar el botón si cambia el estado
    return f'<div class="acciones" data-wa="{esc(wa_link(cfg, a) or "")}">{boton(cfg, a)}{agregar(a)}</div>'


def scripts(raiz):
    return f'<script src="{raiz}datos.js"></script><script src="{raiz}carrito.js"></script>'


def precio_html(cfg, a):
    p = dinero(a.get("precio"), cfg["moneda"])
    cu = " <small>c/u</small>" if a.get("por_unidad") else ""
    s = f'<span class="precio">{p}{cu}</span>' if p else '<span class="precio consultar">Precio a consultar</span>'
    if a.get("precio_nuevo"):
        s += f' <span class="nuevo">Nuevo: {dinero(a["precio_nuevo"], cfg["moneda"])}</span>'
    return s


def sello(a):
    e = a.get("estado")
    return f'<span class="sello {e}">{"VENDIDO" if e == "vendido" else "APARTADO"}</span>' if e in ("vendido", "apartado") else ""


def head(cfg, titulo, desc, img, url, raiz):
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(titulo)}</title><meta name="description" content="{esc(desc)}">
<meta property="og:type" content="website"><meta property="og:title" content="{esc(titulo)}">
<meta property="og:description" content="{esc(desc)}"><meta property="og:url" content="{esc(url)}">
{f'<meta property="og:image" content="{esc(img)}"><meta property="og:image:width" content="1400"><meta name="twitter:card" content="summary_large_image">' if img else ''}
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>📦</text></svg>">
{FONTS}<link rel="stylesheet" href="{raiz}estilo.css"></head><body>"""


def meta_pills(cfg):
    out = []
    if cfg.get("zona") and cfg["zona"].split(",")[0] not in cfg.get("entrega", ""): out.append(f"📍 {esc(cfg['zona'])}")
    if cfg.get("fecha_limite"): out.append(f"🗓️ Disponible hasta el {esc(cfg['fecha_limite'])}")
    if cfg.get("entrega"): out.append(f"🚚 {esc(cfg['entrega'])}")
    if cfg.get("nota_precios"): out.append(f"💬 {esc(cfg['nota_precios'])}")
    return "".join(f'<span class="pill">{p}</span>' for p in out)


def pagina_inicio(cfg, items):
    cats = [c for c in CAT_ORDEN if any(a["categoria"] == c for a in items)]
    disp = sum(a.get("estado") in ("disponible", "apartado") for a in items)
    portada = next((a for a in items if a["_img"]), None)
    cards = []
    for k, a in enumerate(items):
        foto = (f'<img src="img/{a["_img"][0]}-t.jpg" alt="{esc(a["titulo"])}"{" loading=lazy" if k > 3 else ""}>'
                if a["_img"] else '<div class="sinfoto">Foto próximamente</div>')
        cards.append(f"""<article class="card {esc(a.get('estado',''))}" data-cat="{esc(a['categoria'])}" data-slug="{esc(a['slug'])}"{" hidden" if a.get("estado") == "pausado" else ""}>
{sello(a)}<a class="foto" href="a/{a['slug']}/">{foto}</a>
<div class="body"><span class="cat">{esc(a['categoria'])}</span>
<h2><a href="a/{a['slug']}/">{esc(a['titulo'])}</a></h2><div>{precio_html(cfg, a)}</div>{acciones(cfg, a)}</div></article>""")
    botones = '<button aria-pressed="true" data-f="*">Todo</button>' + "".join(
        f'<button aria-pressed="false" data-f="{esc(c)}">{esc(c)}</button>' for c in cats)
    return head(cfg, cfg["titulo"], f"{disp} artículos a la venta por mudanza. Fotos, medidas y precios.",
                f"{cfg['url']}img/{portada['_img'][0]}.jpg" if portada else None, cfg["url"], "") + f"""
<div class="wrap"><header class="hero"><h1>{esc(cfg['titulo'])}</h1>
<p>Me mudo y todo esto busca nuevo hogar. Toca un artículo para ver más fotos y medidas. ¿Te interesa una sola cosa? Usa «Lo quiero». ¿Varias? Agrégalas al carrito y mándame la lista completa por WhatsApp.</p>
<div class="meta">{meta_pills(cfg)}<span class="pill">📦 <span id="ndisp">{disp}</span> disponibles</span></div>
<a class="pdfbtn" id="pdflink" href="{cfg['_pdf']}" download="Venta por mudanza - catalogo.pdf">📄 Descargar catálogo en PDF</a></header>
<nav class="filtros" aria-label="Categorías">{botones}<label class="toggle"><input type="checkbox" id="ov"> Ocultar vendidos</label></nav>
<main class="grid" id="grid">{''.join(cards)}</main>
<footer>¿Dudas o quieres ver algo en persona? Escríbeme por WhatsApp al {esc(wa_bonito(cfg))}. · Descarga el <a href="{cfg['_pdf']}" download="Venta por mudanza - catalogo.pdf">catálogo en PDF</a> para compartirlo.</footer></div>
{f'<a class="fab" id="fab" href="{esc(wa_general(cfg))}" target="_blank" rel="noopener">{WA_ICON} WhatsApp</a>' if wa_general(cfg) else ''}
{scripts("")}<script>
const g=document.getElementById('grid'),bs=document.querySelectorAll('.filtros button');
bs.forEach(b=>b.onclick=()=>{{bs.forEach(x=>x.setAttribute('aria-pressed',x===b));const f=b.dataset.f;
g.querySelectorAll('.card').forEach(c=>c.style.display=(f==='*'||c.dataset.cat===f)?'':'none')}});
window.FILTRO_ACTIVO=()=>document.querySelector('.filtros [aria-pressed=true]');
const ov=document.getElementById('ov');try{{ov.checked=localStorage.getItem('ov')==='1'}}catch(e){{}}
const ap=()=>{{g.classList.toggle('ocultar-vendidos',ov.checked);try{{localStorage.setItem('ov',ov.checked?'1':'0')}}catch(e){{}}}};ov.onchange=ap;ap();
</script></body></html>"""


def pagina_articulo(cfg, a):
    url = f"{cfg['url']}a/{a['slug']}/"
    p = dinero(a.get("precio"), cfg["moneda"])
    titulo = f"{a['titulo']} · {p}" if p else a["titulo"]
    if a.get("estado") == "vendido":
        titulo = f"VENDIDO · {a['titulo']}"
    fotos = "".join(f'<img src="../../img/{n}.jpg" alt="{esc(a["titulo"])} foto {i}" {"loading=lazy" if i > 1 else ""}>'
                    for i, n in enumerate(a["_img"], 1)) or '<div class="sinfoto" style="aspect-ratio:4/3;width:100%;background:var(--line);border-radius:14px">Foto próximamente</div>'
    n = len(a["_img"])
    det = "".join(f"<li>{esc(d)}</li>" for d in a.get("detalles", []))
    ref = f'<p><a href="{esc(a["referencia"])}" target="_blank" rel="noopener">Ver modelo en la tienda ↗</a></p>' if a.get("referencia") else ""
    return head(cfg, titulo, a.get("descripcion") or cfg["titulo"],
                f"{cfg['url']}img/{a['_img'][0]}.jpg" if a["_img"] else None, url, "../../") + f"""
<div class="wrap"><a class="volver" href="../../">← Ver todo</a>
<div class="art"><div><div class="galeria{' una' if n < 2 else ''}">{fotos}</div>
{f'<div class="contador">{n} fotos · desliza para ver más</div>' if n > 1 else ''}</div>
<aside class="panel" style="position:relative" data-slug="{esc(a['slug'])}">{sello(a)}<p class="aviso" {"" if a.get("estado") == "pausado" else "hidden"}>Este artículo no está disponible por ahora.</p><span class="cat">{esc(a['categoria'])}</span>
<h1>{esc(a['titulo'])}</h1><div>{precio_html(cfg, a)}</div>
<p>{esc(a.get('descripcion'))}</p>{f'<ul>{det}</ul>' if det else ''}{ref}
<div class="meta">{meta_pills(cfg)}</div>{acciones(cfg, a)}</aside></div></div>
<div class="barra" data-slug="{esc(a['slug'])}"><div class="bp">{precio_html(cfg, a)}</div>{acciones(cfg, a)}</div>{scripts("../../")}</body></html>"""


# ---------- PDF ----------
PDF_CSS = """
@page{size:Letter;margin:0}
*{box-sizing:border-box}body{margin:0;font:11pt/1.45 -apple-system,"Helvetica Neue",Arial,sans-serif;color:#1f1b16;-webkit-print-color-adjust:exact;print-color-adjust:exact}
.serif{font-family:"Fraunces",Georgia,serif;font-weight:600;letter-spacing:-.01em}
.pag{width:8.5in;height:11in;page-break-after:always;position:relative;overflow:hidden;background:#f6f1ea}
.pag:last-child{page-break-after:auto}
.portada{display:flex;flex-direction:column}
.portada .foto{height:5.2in;background-size:cover;background-position:center}
.portada .txt{flex:1;padding:.45in .6in .5in;display:grid;grid-template-columns:1fr 2.3in;gap:.4in;align-items:center}
.portada h1{font-size:48pt;line-height:1;margin:0 0 12pt}
.portada p{font-size:13pt;color:#4a4238;margin:0 0 10pt}
.portada .qr{background:#fff;padding:12pt;border-radius:12pt;text-align:center;font-size:9.5pt;color:#4a4238}
.portada .qr svg{width:100%;height:auto;display:block;margin-bottom:6pt}
.wa{margin-top:12pt;font-size:14pt;color:#1f8f4e}.wa b{font-size:16pt}
a{color:inherit;text-decoration:none}.qr,.pie{display:block}.it .pie{display:flex}.visitar{display:block;margin-top:8pt;background:#b4532a;color:#fff;font-weight:700;font-size:12pt;padding:9pt 10pt;border-radius:8pt;text-decoration:none}
.url{text-decoration:underline;font-weight:700;color:#b4532a;font-size:12pt;word-break:break-all}
.chips{display:flex;flex-wrap:wrap;gap:6pt;margin-top:10pt}.chips span{border:1px solid #d8ccbd;border-radius:99px;padding:3pt 9pt;font-size:9.5pt;background:#fffdf9}
.cab{display:flex;justify-content:space-between;align-items:baseline;padding:.45in .6in .15in;border-bottom:1px solid #e4dacd}
.cab h2{margin:0;font-size:22pt}.cab span{color:#6f655a;font-size:9.5pt}
table{width:100%;border-collapse:collapse;margin:.15in 0}td{padding:4pt 0;border-bottom:1px solid #e4dacd;font-size:10pt}
td.r{text-align:right;font-weight:700;white-space:nowrap}td.c{color:#6f655a;font-size:8.5pt;width:1.35in;padding-right:8pt}
.vend td{color:#a39888;text-decoration:line-through}
.lista{padding:0 .6in}
.items{padding:.2in .6in .3in;display:flex;flex-direction:column;gap:.22in}
.it{display:grid;grid-template-columns:3.9in 1fr;gap:.25in;background:#fffdf9;border:1px solid #e4dacd;border-radius:10pt;overflow:hidden;height:4.35in;position:relative}
.it .ph{display:grid;grid-template-rows:1fr auto;gap:3pt;background:#e4dacd}
.it .ph .main{background-size:cover;background-position:center}
.it .ph .mini{display:flex;gap:3pt;height:.8in}.it .ph .mini div{flex:1;background-size:cover;background-position:center}
.it .info{padding:.2in .25in .2in 0;display:flex;flex-direction:column}
.it .cat{font-size:8pt;text-transform:uppercase;letter-spacing:.1em;color:#6f655a}
.it h3{font-size:17pt;line-height:1.1;margin:3pt 0 6pt}
.it .pr{font-size:16pt;font-weight:800;color:#b4532a}.it .pr.c{font-size:11pt;font-weight:600;color:#6f655a}
.it .nv{font-size:9pt;color:#6f655a}
.it p{font-size:9.5pt;margin:6pt 0 4pt;color:#3a332a}
.it ul{margin:0;padding-left:13pt;font-size:9pt;color:#3a332a}
.it .pie{margin-top:auto;display:flex;align-items:center;gap:8pt;font-size:8.5pt;color:#6f655a}
.it .pie svg{width:.75in;height:.75in;background:#fff;flex:none}
.sinf{display:grid;place-items:center;color:#6f655a;font-size:10pt}
.sello{position:absolute;top:12pt;left:12pt;background:#8b2c1d;color:#fff;font-weight:800;letter-spacing:.12em;font-size:12pt;padding:4pt 10pt;border-radius:4pt;transform:rotate(-5deg)}
.sello.apartado{background:#b8860b}
.vendido .ph{filter:grayscale(1) opacity(.55)}
.npag{position:absolute;bottom:.25in;right:.6in;font-size:8pt;color:#a39888}
"""


def pdf_html(cfg, items):
    items = [a for a in items if a.get("estado") != "pausado"]
    portada = next((a for a in items if a["_img"]), None)
    chips = "".join(f"<span>{c}</span>" for c in re.findall(r'<span class="pill">(.*?)</span>', meta_pills(cfg)))
    pags = [f"""<section class="pag portada">
<div class="foto" style="background-image:url('img/{portada['_img'][0]}.jpg')"></div>
<div class="txt"><div><h1 class="serif">{esc(cfg['titulo'])}</h1>
<p>Me mudo y todo esto busca nuevo hogar: muebles de sala, comedor, recámara y más, en muy buen estado.</p>
<p>Este catálogo trae fotos, medidas y precios. <b>En la página web siempre está lo más actualizado</b>, incluyendo lo que ya se vendió.</p>
<div class="chips">{chips}</div>{f'<div class="wa">WhatsApp: <a href="{esc(wa_general(cfg))}"><b>{esc(wa_bonito(cfg))}</b></a></div>' if cfg.get("whatsapp") else ""}</div>
<div class="qr"><a href="{esc(cfg['url'])}">{qr_svg(cfg['url'])}</a>Escanea el código o toca el botón<a class="visitar" href="{esc(cfg['url'])}">Visitar página web</a></div></div></section>"""]

    filas = [
        f'<tr class="{"vend" if a.get("estado") == "vendido" else ""}"><td class="c">{esc(a["categoria"])}</td><td><a href="{esc(cfg["url"] + "a/" + a["slug"] + "/")}">{esc(a["titulo"])}</a></td>'
        f'<td class="r">{dinero(a.get("precio"), cfg["moneda"]) or "A consultar"}{" c/u" if a.get("por_unidad") else ""}</td></tr>' for a in items]
    POR_PAG = 36
    for j in range(0, len(filas), POR_PAG):
        pags.append(f"""<section class="pag"><div class="cab"><h2 class="serif">Lista de precios</h2><span>{len(items)} artículos</span></div>
<div class="lista"><table>{"".join(filas[j:j + POR_PAG])}</table></div></section>""")
    base = len(pags) + 1

    for k in range(0, len(items), 2):
        bloques = []
        for a in items[k:k + 2]:
            if a["_img"]:
                mini = "".join(f'<div style="background-image:url(\'img/{n}-t.jpg\')"></div>' for n in a["_img"][1:4])
                ph = f'<div class="ph"><div class="main" style="background-image:url(\'img/{a["_img"][0]}.jpg\')"></div>{f"<div class=mini>{mini}</div>" if mini else ""}</div>'
            else:
                ph = '<div class="ph sinf">Foto próximamente</div>'
            p = dinero(a.get("precio"), cfg["moneda"])
            pr = f'<div class="pr">{p}{" c/u" if a.get("por_unidad") else ""}</div>' if p else '<div class="pr c">Precio a consultar</div>'
            if a.get("precio_nuevo"):
                pr += f'<div class="nv">Precio nuevo: {dinero(a["precio_nuevo"], cfg["moneda"])}</div>'
            det = "".join(f"<li>{esc(d)}</li>" for d in a.get("detalles", [])[:5])
            bloques.append(f"""<div class="it {esc(a.get('estado',''))}">{ph}{sello(a)}<div class="info">
<span class="cat">{esc(a['categoria'])}</span><h3 class="serif"><a href="{esc(cfg['url'] + 'a/' + a['slug'] + '/')}">{esc(a['titulo'])}</a></h3>{pr}
<p>{esc(a.get('descripcion'))}</p>{f'<ul>{det}</ul>' if det else ''}
<a class="pie" href="{esc(cfg['url'] + 'a/' + a['slug'] + '/')}">{qr_svg(cfg['url'] + 'a/' + a['slug'] + '/')}<span>Escanea o toca para ver todas las fotos y escribirme por WhatsApp{(" al " + esc(wa_bonito(cfg))) if cfg.get("whatsapp") else ""}</span></a></div></div>""")
        pags.append(f'<section class="pag"><div class="items">{"".join(bloques)}</div><span class="npag">{esc(cfg["titulo"])} · {k // 2 + base}</span></section>')

    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8">{FONTS}<style>{PDF_CSS}</style></head><body>{''.join(pags)}</body></html>"""


def generar_pdf(cfg, items):
    tmp = SITIO / "_catalogo.html"
    tmp.write_text(pdf_html(cfg, items))
    out = SITIO / "catalogo.pdf"
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-pdf-header-footer",
                    "--virtual-time-budget=8000", f"--print-to-pdf={out}", tmp.as_uri()],
                   check=True, capture_output=True)
    tmp.unlink()
    return out


PUBLICOS = ("slug", "titulo", "categoria", "precio", "precio_lista", "precio_nuevo", "por_unidad", "unidades",
            "descripcion", "detalles", "referencia", "_img")


def pdf_version(sitio):
    pdf = sitio / "catalogo.pdf"
    return hashlib.md5(pdf.read_bytes()).hexdigest()[:8] if pdf.exists() else "0"


def guardar_pdf_version(sitio, est=None, firma=""):
    # "hoja" = firma del estado/precios con que se hizo el PDF, para no regenerarlo si nada cambió
    (sitio / "pdf.json").write_text(json.dumps({"v": pdf_version(sitio), "hoja": firma}) + "\n")


def main_ci(sitio):
    """GitHub Actions: regenera el PDF con el estado actual, sin fotos originales."""
    global SITIO
    SITIO = Path(sitio).resolve()
    cat = json.loads((SITIO / "catalogo.json").read_text())
    cfg, items = cat["config"], cat["articulos"]
    for a in items:
        a["precio"] = a.get("precio_lista", a.get("precio"))
    est = leer_estado(SITIO)
    hoja = leer_hoja(cfg)
    firma = hashlib.md5(json.dumps(hoja, sort_keys=True).encode()).hexdigest()[:8] if hoja else ""
    previo = json.loads((SITIO / "pdf.json").read_text()) if (SITIO / "pdf.json").exists() else {}
    if hoja and previo.get("hoja") == firma and "--forzar" not in sys.argv:
        print("Sin cambios en la hoja; no se regenera el PDF.")
        return
    aplicar_hoja(items, est, hoja)
    aplicar_precios(items, leer_precios(SITIO))
    aplicar_estado(items, est)
    generar_pdf(cfg, items)
    guardar_pdf_version(SITIO, est, firma)
    print("PDF regenerado:", pdf_version(SITIO))


def main():
    if "--ci" in sys.argv:
        return main_ci(sys.argv[sys.argv.index("--ci") + 1])
    cfg, items, avisos = cargar()
    if not cfg["url"].endswith("/"):
        cfg["url"] += "/"
    SITIO.mkdir(exist_ok=True)
    est = leer_estado(SITIO)
    for a in items:
        a.setdefault("slug", slugify(a["carpeta"]) if a.get("carpeta") else slugify(a["titulo"]))
    desconocidos = set(est["estados"]) - {a["slug"] for a in items}
    for s_ in desconocidos:
        avisos.append(f"estado.json menciona un artículo que ya no existe: {s_}")
    # lista para la hoja de control (IMPORTDATA): orden FIJO de alta, nunca se reordena,
    # para que las columnas que llena el usuario sigan alineadas con su artículo
    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "Artículo", "Precio lista"])
    for a in items:
        w.writerow([a["slug"], a["titulo"], a.get("precio") if a.get("precio") is not None else ""])
    (SITIO / "lista.csv").write_text(buf.getvalue())
    hoja = leer_hoja(cfg)
    aplicar_hoja(items, est, hoja)
    if hoja:
        (SITIO / "estado.json").write_text(json.dumps(est, ensure_ascii=False, indent=2) + "\n")
    firma = hashlib.md5(json.dumps(hoja, sort_keys=True).encode()).hexdigest()[:8] if hoja else ""
    for a in items:
        a["precio_lista"] = a.get("precio")
    precios = leer_precios(SITIO)
    for s_ in set(precios) - {a["slug"] for a in items}:
        avisos.append(f"precios.json menciona un artículo que ya no existe: {s_}")
    aplicar_precios(items, precios)
    aplicar_estado(items, est)
    procesar_fotos(items)
    (SITIO / "estilo.css").write_text(CSS)
    (SITIO / ".nojekyll").write_text("")
    pub_cfg = {k: v for k, v in cfg.items() if not k.startswith("_")}
    (SITIO / "catalogo.json").write_text(json.dumps(
        {"config": pub_cfg, "categorias": CAT_ORDEN,
         "articulos": [{k: a[k] for k in PUBLICOS if k in a} for a in items]},
        ensure_ascii=False, indent=1))
    datos = {"num": re.sub(r"\D", "", cfg.get("whatsapp", "")), "url": cfg["url"], "moneda": cfg["moneda"],
             "hoja": cfg.get("hoja_csv", ""),
             "items": {a["slug"]: {"t": a["titulo"], "p": a.get("precio"), "img": a["_img"][0] if a["_img"] else None,
                                   "max": a.get("unidades", 1) if a.get("por_unidad") else 1}
                       for a in items}}
    (SITIO / "datos.js").write_text("window.VENTA=" + json.dumps(datos, ensure_ascii=False) + ";")
    (SITIO / "carrito.js").write_text((RAIZ / "carrito.js").read_text())
    (SITIO / "tools").mkdir(exist_ok=True)
    shutil.copy(RAIZ / "build.py", SITIO / "tools" / "build.py")
    wf = SITIO / ".github" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    shutil.copy(RAIZ / "control-workflow.yml", wf / "control.yml")
    (wf / "pdf.yml").unlink(missing_ok=True)
    shutil.copy(RAIZ / "control_robot.py", SITIO / "tools" / "control_robot.py")
    (SITIO / "control").mkdir(exist_ok=True)
    (SITIO / "control" / "index.html").write_text((RAIZ / "control.html").read_text())
    if "--sin-pdf" not in sys.argv:
        generar_pdf(cfg, items)
        guardar_pdf_version(SITIO, est, firma)
    cfg["_pdf"] = "catalogo.pdf?v=" + pdf_version(SITIO)
    (SITIO / "index.html").write_text(pagina_inicio(cfg, items))
    if (SITIO / "a").exists():
        shutil.rmtree(SITIO / "a")
    for a in items:
        d = SITIO / "a" / a["slug"]
        d.mkdir(parents=True)
        (d / "index.html").write_text(pagina_articulo(cfg, a))
    print(f"OK: {len(items)} artículos, {sum(len(a['_img']) for a in items)} fotos")
    for v in avisos:
        print("  -", v)


if __name__ == "__main__":
    main()
