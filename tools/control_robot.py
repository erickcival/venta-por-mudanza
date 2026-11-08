#!/usr/bin/env python3
"""Robot del centro de control (corre en GitHub Actions al abrir un issue "[control] ...").

Lee el bloque ```json``` del issue: {"estados": {slug: estado}, "precios": {slug: precio|null}}
y actualiza estado.json y precios.json. Escribe un resumen en $RESUMEN para comentarlo en el issue.
Nunca ejecuta nada del texto del issue: solo lo parsea como JSON y valida cada valor.
"""
import json, os, re, sys
from pathlib import Path

ESTADOS = {"disponible": "Disponible", "apartado": "Apartado", "vendido": "Vendido", "pausado": "En pausa"}
resumen = Path(os.environ.get("RESUMEN", "/tmp/resumen.md"))

ev = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
body = (ev.get("issue") or {}).get("body") or ""
m = re.search(r"```json\s*(\{.*?\})\s*```", body, re.S)
if not m:
    resumen.write_text("No encontré cambios en este issue (falta el bloque de datos). No se modificó nada.")
    sys.exit(1)
try:
    cambios = json.loads(m.group(1))
    assert isinstance(cambios, dict)
except Exception:
    resumen.write_text("Los datos del issue no son válidos. No se modificó nada.")
    sys.exit(1)

cat = {a["slug"]: a for a in json.loads(Path("catalogo.json").read_text())["articulos"]}
est = json.loads(Path("estado.json").read_text()) if Path("estado.json").exists() else {"estados": {}}
est.setdefault("estados", {})
precios = json.loads(Path("precios.json").read_text()) if Path("precios.json").exists() else {}
fmt = lambda n: f"${n:,.0f}"
hechos, avisos = [], []

for slug, e in (cambios.get("estados") or {}).items():
    if slug not in cat:
        avisos.append(f"- `{slug}`: ese artículo no existe"); continue
    if e not in ESTADOS:
        avisos.append(f"- {cat[slug]['titulo']}: estado no válido"); continue
    if e == "disponible":
        est["estados"].pop(slug, None)
    else:
        est["estados"][slug] = e
    hechos.append(f"- {cat[slug]['titulo']}: **{ESTADOS[e]}**")

for slug, p in (cambios.get("precios") or {}).items():
    if slug not in cat:
        avisos.append(f"- `{slug}`: ese artículo no existe"); continue
    lista = cat[slug].get("precio_lista")
    if p is None:                       # volver al precio original
        if lista is None:
            precios.pop(slug, None)
        else:
            precios[slug] = lista
        hechos.append(f"- {cat[slug]['titulo']}: precio de vuelta a **{fmt(lista) if lista else 'a consultar'}**")
        continue
    try:
        p = round(float(p), 2)
        assert 0 < p < 10_000_000
    except Exception:
        avisos.append(f"- {cat[slug]['titulo']}: precio no válido"); continue
    precios[slug] = int(p) if p == int(p) else p
    hechos.append(f"- {cat[slug]['titulo']}: precio **{fmt(p)}**")

est["estados"] = dict(sorted(est["estados"].items()))
Path("estado.json").write_text(json.dumps(est, ensure_ascii=False, indent=2) + "\n")
Path("precios.json").write_text(json.dumps(dict(sorted(precios.items())), ensure_ascii=False, indent=2) + "\n")

txt = "✅ Cambios aplicados. La página se actualiza en ~1 minuto y el PDF en ~2.\n\n" + "\n".join(hechos or ["(ninguno)"])
if avisos:
    txt += "\n\n⚠️ No se aplicaron:\n" + "\n".join(avisos)
resumen.write_text(txt)
print(txt)
