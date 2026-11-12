// Carrito: guarda la selección en este navegador y arma un mensaje de WhatsApp con todo.
(function () {
  const V = window.VENTA;
  if (!V) return;
  const BASE = document.currentScript.src.replace(/carrito\.js.*$/, "");
  const KEY = "venta-carrito";
  const fmt = (n) => "$" + Math.round(n).toLocaleString("en-US");
  const corto = (t) => t.replace(/\s*\([^)]*disponibles?\)\s*$/i, "");
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // estado en vivo (lo edita el centro de control): disponible / apartado / vendido / pausado
  let ESTADOS = {};
  const estadoDe = (k) => ESTADOS[k] || "disponible";
  const vendible = (k) => V.items[k] && ["disponible", "apartado"].includes(estadoDe(k));

  let mem = {};
  const leer = () => {
    try { mem = JSON.parse(localStorage.getItem(KEY) || "{}") || {}; } catch (e) {}
    for (const k of Object.keys(mem)) if (!vendible(k)) delete mem[k]; // vendidos, pausados o eliminados
    return mem;
  };
  const guardar = () => { try { localStorage.setItem(KEY, JSON.stringify(mem)); } catch (e) {} };
  leer();

  // ---- UI fija ----
  const enArticulo = !!document.querySelector(".barra");
  const pill = document.createElement("button");
  pill.type = "button";
  pill.className = "carro " + (enArticulo ? "arriba" : "abajo");
  pill.hidden = true;
  document.body.appendChild(pill);

  const dlg = document.createElement("dialog");
  dlg.className = "dlg";
  dlg.setAttribute("aria-label", "Carrito");
  document.body.appendChild(dlg);
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });
  pill.onclick = () => { pintarDlg(); dlg.showModal(); };

  function resumen() {
    let n = 0, total = 0, sinPrecio = 0;
    for (const [k, q] of Object.entries(mem)) {
      const it = V.items[k];
      n += q;
      if (it.p == null) sinPrecio++; else total += it.p * q;
    }
    return { n, total, sinPrecio };
  }

  function mensaje() {
    const lineas = ["Hola, me interesan estos artículos de tu venta por mudanza:", ""];
    let i = 1;
    for (const [k, q] of Object.entries(mem)) {
      const it = V.items[k];
      const cant = q > 1 ? ` (x${q})` : "";
      const p = it.p == null ? "precio a consultar" : fmt(it.p * q);
      lineas.push(`${i++}. ${corto(it.t)}${cant}: ${p}`);
    }
    const r = resumen();
    lineas.push("", `Total: ${fmt(r.total)} ${V.moneda}` + (r.sinPrecio ? ` + ${r.sinPrecio} a consultar` : ""));
    lineas.push("", "¿Siguen disponibles?");
    return lineas.join("\n");
  }

  function pintarDlg() {
    const filas = Object.entries(mem).map(([k, q]) => {
      const it = V.items[k];
      const img = it.img ? `<img src="${BASE}img/${it.img}-t.jpg" alt="">` : '<div class="ph"></div>';
      const precio = it.p == null ? "Precio a consultar" : fmt(it.p * q) + (q > 1 ? ` <small>(${fmt(it.p)} c/u)</small>` : "");
      const qty = it.max > 1
        ? `<div class="qty"><button type="button" data-m="${k}" aria-label="Quitar uno">−</button><span>${q}</span><button type="button" data-p="${k}" aria-label="Agregar uno" ${q >= it.max ? "disabled" : ""}>+</button></div>`
        : "";
      return `<li>${img}<div class="t">${esc(corto(it.t))}<b>${precio}</b></div><div class="ctl">${qty}<button type="button" class="quitar" data-x="${k}">Quitar</button></div></li>`;
    }).join("");
    const r = resumen();
    const href = V.num ? `https://wa.me/${V.num}?text=${encodeURIComponent(mensaje())}` : null;
    dlg.innerHTML = `<header><h2>Tu carrito</h2><button type="button" class="x" aria-label="Cerrar">×</button></header>` +
      (r.n ? `<ul>${filas}</ul><footer><div class="tot"><span>Total</span><span>${fmt(r.total)} ${V.moneda}</span></div>` +
        `<p class="nota">${r.sinPrecio ? r.sinPrecio + " artículo(s) con precio a consultar. " : ""}Se manda la lista completa por WhatsApp; ahí acordamos entrega y pago.</p>` +
        (href ? `<a class="btn" href="${href}" target="_blank" rel="noopener">Enviar lista por WhatsApp</a>` : "") + `</footer>`
        : `<div class="vacio">Tu carrito está vacío.<br>Toca «🛒 Agregar» en lo que te interese.</div>`);
    dlg.querySelector(".x").onclick = () => dlg.close();
    dlg.querySelectorAll("[data-x]").forEach((b) => (b.onclick = () => { delete mem[b.dataset.x]; cambio(); pintarDlg(); }));
    dlg.querySelectorAll("[data-p]").forEach((b) => (b.onclick = () => { const k = b.dataset.p; mem[k] = Math.min(V.items[k].max, mem[k] + 1); cambio(); pintarDlg(); }));
    dlg.querySelectorAll("[data-m]").forEach((b) => (b.onclick = () => { const k = b.dataset.m; mem[k]--; if (mem[k] < 1) delete mem[k]; cambio(); pintarDlg(); }));
  }

  function cambio() {
    guardar();
    const r = resumen();
    pill.hidden = r.n === 0;
    pill.innerHTML = `🛒 ${r.n} ${r.n === 1 ? "artículo" : "artículos"} · ${fmt(r.total)}${enArticulo ? "" : " · Ver y enviar"}`;
    document.body.classList.toggle("con-carro", r.n > 0);
    document.querySelectorAll("[data-add]").forEach((b) => {
      const dentro = !!mem[b.dataset.add];
      b.setAttribute("aria-pressed", dentro);
      b.textContent = dentro ? "✓ En carrito" : "🛒 Agregar";
    });
  }

  // ---- aplicar estado en vivo a la página ----
  const WA_SVG = (document.querySelector("a.btn svg") || {}).outerHTML || "";
  function aplicarEstado() {
    document.querySelectorAll(".card[data-slug], .panel[data-slug]").forEach((el) => {
      const e = estadoDe(el.dataset.slug);
      if (el.classList.contains("card")) {
        el.classList.remove("disponible", "apartado", "vendido", "pausado");
        el.classList.add(e);
        el.hidden = e === "pausado";
      }
      let s = el.querySelector(":scope > .sello");
      if (e === "vendido" || e === "apartado") {
        if (!s) { s = document.createElement("span"); el.prepend(s); }
        s.className = "sello " + e;
        s.textContent = e === "vendido" ? "VENDIDO" : "APARTADO";
      } else if (s) s.remove();
      const av = el.querySelector(".aviso");
      if (av) av.hidden = e !== "pausado";
    });
    document.querySelectorAll(".acciones").forEach((ac) => {
      const host = ac.closest("[data-slug]");
      if (!host) return;
      const e = estadoDe(host.dataset.slug);
      const apagado = e === "vendido" || e === "pausado";
      const viejo = ac.querySelector(".btn");
      let nuevo;
      if (apagado || !ac.dataset.wa) {
        nuevo = document.createElement("span");
        nuevo.className = "btn"; nuevo.setAttribute("aria-disabled", "true");
        nuevo.textContent = e === "vendido" ? "Vendido" : e === "pausado" ? "No disponible" : "Contacto próximamente";
      } else {
        nuevo = document.createElement("a");
        nuevo.className = "btn"; nuevo.href = linkWA(host.dataset.slug) || ac.dataset.wa; nuevo.target = "_blank"; nuevo.rel = "noopener";
        nuevo.innerHTML = WA_SVG + " Lo quiero";
      }
      if (viejo) viejo.replaceWith(nuevo);
      const add = ac.querySelector("[data-add]");
      if (add) add.hidden = apagado;
    });
    // vendidos siempre al final de la lista (respeta el orden original entre ellos)
    const grid = document.getElementById("grid");
    if (grid) {
      const cards = [...grid.querySelectorAll(":scope > .card[data-slug]")];
      cards.forEach((c, i) => { if (!c.dataset.ord) c.dataset.ord = i; });
      cards.sort((a, b) => (estadoDe(a.dataset.slug) === "vendido") - (estadoDe(b.dataset.slug) === "vendido") || a.dataset.ord - b.dataset.ord)
        .forEach((c) => grid.appendChild(c));
    }
    const nd = document.getElementById("ndisp");
    if (nd) nd.textContent = Object.keys(V.items).filter(vendible).length;
  }

  // CSV de la hoja de control publicada: columnas id, estado, precio
  function parseCSV(txt) {
    const filas = []; let f = [], c = "", q = false;
    for (let i = 0; i < txt.length; i++) {
      const ch = txt[i];
      if (q) { if (ch === '"' && txt[i + 1] === '"') { c += '"'; i++; } else if (ch === '"') q = false; else c += ch; }
      else if (ch === '"') q = true;
      else if (ch === ",") { f.push(c); c = ""; }
      else if (ch === "\n" || ch === "\r") { if (ch === "\r" && txt[i + 1] === "\n") i++; f.push(c); filas.push(f); f = []; c = ""; }
      else c += ch;
    }
    if (c || f.length) { f.push(c); filas.push(f); }
    return filas;
  }
  function desdeHoja(txt) {
    const filas = parseCSV(txt);
    const cab = (filas.shift() || []).map((h) => h.trim().toLowerCase());
    const iId = cab.indexOf("id"), iE = cab.indexOf("estado"), iP = cab.indexOf("precio");
    if (iId < 0) return null;
    const est = {}, precios = {};
    for (const f of filas) {
      const k = (f[iId] || "").trim();
      if (!k || !V.items[k]) continue;
      let e = (f[iE] || "").trim().toLowerCase();
      e = { pausa: "pausado", "": "disponible" }[e] || e;
      if (e !== "disponible") est[k] = e;
      const p = parseFloat(String(f[iP] || "").replace(/[$,\s]/g, ""));
      if (p > 0) precios[k] = p;
    }
    return { est, precios };
  }
  function aplicarPrecios(precios) {
    const moneda = V.moneda || "MXN";
    for (const [k, p] of Object.entries(precios)) {
      if (!V.items[k] || V.items[k].p === p) continue;
      V.items[k].p = p;
      document.querySelectorAll(`[data-slug="${k}"] .precio`).forEach((el) => {
        const cu = el.querySelector("small") ? " <small>c/u</small>" : "";
        el.classList.remove("consultar");
        el.innerHTML = fmt(p) + " " + moneda + cu;
      });
    }
  }
  function linkWA(k) {
    const it = V.items[k];
    if (!V.num || !it) return "";
    const p = it.p == null ? "" : ` (${fmt(it.p)} ${V.moneda})`;
    return `https://wa.me/${V.num}?text=` + encodeURIComponent(`Hola, me interesa: ${it.t}${p}\n${V.url}a/${k}/`);
  }

  function cargarEstado() {
    const t = Date.now();
    const json = (f) => fetch(BASE + f + "?t=" + t, { cache: "no-store" }).then((r) => (r.ok ? r.json() : null)).catch(() => null);
    const deJSON = () => Promise.all([json("estado.json"), json("precios.json")])
      .then(([e, p]) => (e || p ? { est: (e && e.estados) || {}, precios: p || {} } : null));
    const fuente = V.hoja
      ? fetch(V.hoja + (V.hoja.includes("?") ? "&" : "?") + "t=" + t, { cache: "no-store" })
          .then((r) => (r.ok ? r.text() : Promise.reject())).then(desdeHoja).then((d) => d || deJSON()).catch(deJSON)
      : deJSON();
    fuente
      .then((d) => { if (d) { ESTADOS = d.est; aplicarPrecios(d.precios); aplicarEstado(); leer(); cambio(); } })
      .catch(() => {});
    fetch(BASE + "pdf.json?t=" + t, { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => {
        if (j && j.v) document.querySelectorAll('a[href^="' + BASE + 'catalogo.pdf"], a[href^="catalogo.pdf"]')
          .forEach((a) => (a.href = BASE + "catalogo.pdf?v=" + j.v));
      })
      .catch(() => {});
  }

  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-add]");
    if (!b) return;
    const k = b.dataset.add;
    if (!vendible(k)) return;
    if (mem[k]) delete mem[k]; else mem[k] = 1;
    cambio();
  });
  window.addEventListener("storage", () => { leer(); cambio(); }); // sincroniza entre pestañas
  window.addEventListener("pageshow", () => { leer(); cambio(); }); // al volver con "atrás"
  cambio();
  cargarEstado();
})();
