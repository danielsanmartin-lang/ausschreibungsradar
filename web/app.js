'use strict';

const POR_PAGINA = 100;
let offset = 0;
let ultimoTotal = 0;

const $ = (id) => document.getElementById(id);

// El idioma en una constante y no repetido en cada llamada. No es un mecanismo de
// traducción: es quitar un literal que estaba copiado cuarenta y siete veces.
const LOC = 'de-DE';

const eur = new Intl.NumberFormat(LOC, {
  style: 'currency', currency: 'EUR', maximumFractionDigits: 0,
});

function fmtImporte(v) {
  return (v === null || v === undefined) ? 'kein Wert angegeben' : eur.format(v);
}

function fmtFecha(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return iso.slice(0, 10);
  // Numérica y no con el mes abreviado: es el formato alemán por defecto y ocupa menos,
  // que importa porque las píldoras de la bandeja no pueden partir la línea.
  return d.toLocaleDateString(LOC, { day: '2-digit', month: '2-digit', year: 'numeric' });
}

// Estado del PROCEDIMIENTO tal y como lo guarda la base (model.ESTADO_*) -> lo que se
// enseña. Los valores no se traducen: viajan en las consultas y se comparan en SQL.
const ETIKETTEN_STATUS = {
  vorinformation: 'Vorinformation',
  veroeffentlicht: 'Veröffentlicht',
  in_wertung: 'In Wertung',
  zuschlag: 'Zuschlag erteilt',
  vergeben: 'Vertrag geschlossen',
  aufgehoben: 'Aufgehoben',
  zurueckgezogen: 'Zurückgezogen',
  unbekannt: 'Unbekannt',
};

// Espejo de progreso.FASE_*. Tampoco se traducen: son marcadores que compara el código,
// no texto que lea nadie. La frase que sí se lee la redacta progreso.py.
const FASES = { descargando: 'descargando', zip: 'leyendo el zip' };

// --- Aviso de carga --------------------------------------------------------

// Con histórico en la base, algunas consultas recorren cientos de miles de filas y
// la pestaña tarda en pintarse. Antes no se avisaba de nada y, peor, el contenedor
// se vaciaba DESPUÉS de recibir los datos: durante esos segundos seguía en pantalla
// el contenido de la pestaña anterior y la aplicación parecía congelada.
//
// Tres detalles son los que hacen que el aviso ayude en vez de estorbar:
//   - el contenedor se vacía ANTES de pedir los datos, no después;
//   - el aviso no se pinta hasta pasados 150 ms, para que una consulta rápida no
//     provoque un parpadeo;
//   - cada contenedor lleva número de pasada, y la respuesta que llega tarde
//     —porque ya se ha cambiado de pestaña o de ventana— se descarta en lugar de
//     pintarse encima de lo que se está mirando.
const RETARDO_CARGANDO = 150;
const pasadas = new Map();

function empezarCarga(idContenedor, texto = 'Wird geladen …') {
  const cont = $(idContenedor);
  const pasada = (pasadas.get(idContenedor) || 0) + 1;
  pasadas.set(idContenedor, pasada);

  cont.innerHTML = '';
  cont.setAttribute('aria-busy', 'true');
  const temporizador = setTimeout(() => {
    if (pasadas.get(idContenedor) === pasada) {
      cont.innerHTML = `<p class="cargando">${texto}</p>`;
    }
  }, RETARDO_CARGANDO);

  // `terminar()` devuelve false si esta carga ya está pisada por otra posterior:
  // quien llama tiene que abandonar sin pintar.
  return {
    terminar() {
      clearTimeout(temporizador);
      if (pasadas.get(idContenedor) !== pasada) return false;
      cont.removeAttribute('aria-busy');
      cont.innerHTML = '';
      return true;
    },
  };
}

let soloNovedades = false;
let cierranEnDias = '';
let kpiActivo = 'en_plazo';

// Cada contador de la cabecera y el filtro que aplica. Tiene que coincidir con lo
// que calcula consultas.resumen() o el número volvería a no cuadrar con la lista.
// `clave` y `filtro` son del servidor y no se tocan; solo cambia `etiqueta`.
const KPIS = [
  { clave: 'en_plazo', etiqueta: 'laufend', filtro: { vivas: '1' } },
  { clave: 'cierran_7_dias', etiqueta: 'Frist ≤7 T', clase: 'urgente',
    filtro: { vivas: '1', cierran_en_dias: '7' } },
  { clave: 'sin_revisar', etiqueta: 'ungeprüft', filtro: { vivas: '0', estado: 'nuevo' } },
  { clave: 'siguiendo', etiqueta: 'beobachtet', filtro: { vivas: '0', estado: 'siguiendo' } },
  { clave: 'coincidencias', etiqueta: 'Treffer', filtro: { vivas: '0' } },
];

function fmtFechaHora(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return iso.slice(0, 16).replace('T', ' ');
  return d.toLocaleString(LOC, {
    day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

function filtros() {
  return {
    q: $('q').value.trim(),
    perfil: $('perfil').value,
    estado: $('estado').value,
    bundesland: $('bundesland').value,
    importe_min: $('importe_min').value,
    orden: $('orden').value,
    vivas: $('vivas').checked ? '1' : '0',
    novedades: soloNovedades ? '1' : '',
    cierran_en_dias: cierranEnDias,
  };
}

function aplicarKpi(clave) {
  const kpi = KPIS.find((k) => k.clave === clave);
  if (!kpi) return;
  kpiActivo = clave;
  cierranEnDias = kpi.filtro.cierran_en_dias || '';
  $('vivas').checked = kpi.filtro.vivas === '1';
  $('estado').value = kpi.filtro.estado || '';
  mostrarVista('bandeja');
  cargarLista();
  for (const b of $('kpis').querySelectorAll('button.kpi')) {
    b.classList.toggle('activo', b.dataset.kpi === clave);
  }
}

function limpiarFiltros() {
  kpiActivo = null;
  cierranEnDias = '';
  soloNovedades = false;
  $('q').value = '';
  $('perfil').value = '';
  $('estado').value = '';
  $('bundesland').value = '';
  $('importe_min').value = '';
  $('vivas').checked = false;
  for (const b of $('kpis').querySelectorAll('button.kpi')) b.classList.remove('activo');
  cargarLista();
}

// El desplegable de triaje y la casilla de plazo se combinan, y eso no se veía:
// se elegía "todo menos descartadas" y seguían saliendo solo 57 porque "solo
// abiertas" seguía marcada. Ahora los filtros activos se dicen en voz alta.
function describirFiltros() {
  const partes = [];
  if ($('vivas').checked) partes.push('nur laufende (Frist nicht abgelaufen)');
  if (cierranEnDias) partes.push(`Frist in ${cierranEnDias} Tagen`);
  // `data-chip` en vez de bajar a minúsculas el texto de la opción: en alemán eso
  // destroza los sustantivos («Nicht geprüft» -> «nicht geprüft»).
  const estado = $('estado');
  if (estado.value) {
    const o = estado.selectedOptions[0];
    partes.push(o.dataset.chip || o.text);
  }
  if ($('perfil').value) partes.push($('perfil').value);
  if ($('bundesland').value) partes.push($('bundesland').value);
  if ($('importe_min').value) {
    partes.push(`ab ${eur.format(Number($('importe_min').value))}`);
  }
  if (soloNovedades) partes.push('nur neue');
  if ($('q').value.trim()) partes.push(`texto “${$('q').value.trim()}”`);
  return partes;
}

function query(extra = {}) {
  const p = new URLSearchParams();
  const f = { ...filtros(), ...extra };
  for (const [k, v] of Object.entries(f)) if (v) p.set(k, v);
  return p.toString();
}

// --- Resumen y cabecera ----------------------------------------------------

// --- Qué cubre y qué no ----------------------------------------------------
//
// Los límites de la herramienta, siempre a mano y plegados. Las cifras salen del dato y
// no escritas a mano —si TED empezara a publicar región, el número baja solo—; el resto
// es prosa fija porque no depende de la base. Ningún porcentaje cableado aquí: eso
// envejece en silencio y acaba mintiendo.
const COBERTURA_ABIERTA = 'cobertura-abierta';

function pintarCobertura(c) {
  if (!c) return;
  const caja = $('cobertura');
  const sinCom = c.sin_comunidad.toLocaleString(LOC);
  const total = c.expedientes.toLocaleString(LOC);
  $('cobertura-resumen').textContent =
    `Was dieser Radar abdeckt – und was nicht · ${sinCom} von ${total} Vorgängen ohne Bundesland`;

  // Los nombres salen del dato y no de una lista escrita a mano: así una fuente
  // nueva aparece sola en vez de quedarse muda.
  const NOMBRE_FUENTE = {
    oeffentlichevergabe: 'Bekanntmachungsservice',
    'oeffentlichevergabe:markterkundung': 'Markterkundungen',
    ted: 'TED',
  };
  const historico = (c.fuentes || [])
    .filter((f) => f.desde)
    .map((f) => `${NOMBRE_FUENTE[f.fuente] || f.fuente} ab ${f.desde}`).join(', ');

  const puntos = [
    [`TED veröffentlicht keine Region.`,
     ` ${sinCom} von ${total} Vorgängen bleiben ohne Bundesland ` +
     `(${c.sin_comunidad_ted.toLocaleString(LOC)} davon aus TED): sie fehlen in der ` +
     `Verteilung nach Bundesland und im Gebietsfilter.`],
    ['Das Bundesland ist das der Vergabestelle, nicht das des Leistungsorts.',
     ' Zentrale Beschaffungen des Bundes werden in Bonn gezeichnet (Beschaffungsamt des BMI), ' +
     '„Nordrhein-Westfalen“ ist dort also nicht der Landesmarkt, sondern der Bund.'],
    ['Das Archiv ist ungleich verteilt.',
     historico ? ` ${historico}.` : ' Es ist noch keine Historie geladen.'],
    ['Es wird nicht in den Vergabeunterlagen gesucht.',
     ' Nur Titel, Auftragsgegenstand und Losbeschreibung – und die Unterlagen liegen bei vielen Portalen ohnehin erst nach Registrierung vor, ' +
     'sodass eine Ausschreibung, die dein Produkt nur im Dokument nennt, hier nicht auftaucht.'],
    ['Unterschwellige Direktaufträge sind nicht vollständig erfasst.',
     ' Sie werden oft gar nicht veröffentlicht, und wo doch, selten mit genug Angaben, um sie zu bewerten.'],
    ['Bei der Verteilung nach Bundesland fallen sehr große Aufträge aus den Balken.',
     ' Sie passen nicht in dieselbe Skala; sie stehen unter der Grafik mit Namen und Wert.'],
  ];
  const lista = $('cobertura-lista');
  lista.textContent = '';
  for (const [fuerte, resto] of puntos) {
    const li = document.createElement('li');
    li.innerHTML = '<b></b><span></span>';
    li.querySelector('b').textContent = fuerte;
    li.querySelector('span').textContent = resto;
    lista.appendChild(li);
  }

  // Se pinta una vez y se recuerda cómo lo dejaste. El listener se pone solo la primera
  // vez: `cargarResumen()` corre cada 12 s durante una búsqueda y si no, se acumularían.
  if (!caja.dataset.listo) {
    try {
      caja.open = localStorage.getItem(COBERTURA_ABIERTA) === '1';
    } catch { /* sin almacén, plegado y ya está */ }
    caja.addEventListener('toggle', () => {
      try {
        localStorage.setItem(COBERTURA_ABIERTA, caja.open ? '1' : '0');
      } catch { /* nada que hacer: se plegará al recargar */ }
    });
    caja.dataset.listo = '1';
  }
}

// --- Tiefe des Archivs -----------------------------------------------------
//
// Ampliar el archivo es la única acción de la aplicación que descarga gigabytes, así que
// se comporta como tal: dice el precio antes, pide confirmación, y no se ofrece siquiera
// mientras hay una ingesta en marcha. La ventana por defecto no se toca desde aquí;
// encogerla no tendría sentido —lo descargado ya está en la base— y por eso el único
// movimiento posible es hacia arriba.

// Años en vez de meses cuando la cuenta es redonda, que es como se habla de esto. La
// forma es la de nominativo/acusativo («2 Jahre»), y las frases de abajo están escritas
// para no necesitar ninguna otra: en alemán, meter esto en un dativo obliga a declinarlo
// («mit 2 Jahren») y basta una frase nueva para que la concordancia deje de cuadrar.
function fmtVentana(meses) {
  if (meses % 12 === 0) {
    const anios = meses / 12;
    return anios === 1 ? '1 Jahr' : `${anios} Jahre`;
  }
  return meses === 1 ? '1 Monat' : `${meses} Monate`;
}

function pintarHistorico(h) {
  const caja = $('historico');
  if (!h) { caja.hidden = true; return; }
  caja.hidden = false;

  const texto = $('historico-texto');
  texto.innerHTML = '<b></b><span></span>';
  texto.querySelector('b').textContent = `Historie: ${fmtVentana(h.meses)}.`;

  const boton = $('historico-ampliar');
  const estado = $('historico-estado');
  if (!h.ampliable) {
    texto.querySelector('span').textContent =
      ` Das ist die größte Tiefe, die dieser Radar abruft (${h.maximo} Monate).`;
    boton.hidden = true;
    estado.textContent = '';
    return;
  }

  texto.querySelector('span').textContent =
    ` Erweiterbar auf ${fmtVentana(h.maximo)}: ältere Vergaben tauchen dann ` +
    'in den Vertragsenden, bei den Auftragnehmern und in der Auswertung auf. ' +
    'Dafür werden weitere Daten heruntergeladen' +
    (h.coste_ampliar
      ? `: pro zusätzlichem Jahr ${h.coste_ampliar}. Der Platz bleibt danach auf der `
        + 'Festplatte belegt.'
      : ', was Zeit und Plattenplatz kostet.');

  boton.hidden = false;
  boton.textContent = `Auf ${fmtVentana(h.maximo)} erweitern`;
  // El destino y el precio viajan en el propio botón: el listener se pone una sola vez y
  // no puede quedarse con los valores del primer refresco.
  boton.dataset.meses = String(h.maximo);
  boton.dataset.coste = h.coste_ampliar || '';
  // Esto se repinta cada 12 s mientras hay una ingesta, así que sin esta guarda el botón
  // volvería a habilitarse solo y el segundo clic se comería un 409 que no hacía falta
  // provocar.
  boton.disabled = hayBusqueda;
  estado.textContent = hayBusqueda
    ? 'Es läuft bereits ein Abruf.'
    : 'Der Abruf läuft dann im Hintergrund; du kannst den Radar dabei weiter benutzen.';

  // El listener se pone una sola vez: `cargarResumen()` vuelve cada 12 s mientras hay
  // una búsqueda, y si no se acumularían hasta disparar varias descargas de un clic.
  if (!caja.dataset.listo) {
    boton.addEventListener('click', () => ampliarHistorico());
    caja.dataset.listo = '1';
  }
}

async function ampliarHistorico() {
  const boton = $('historico-ampliar');
  const estado = $('historico-estado');
  const meses = Number(boton.dataset.meses || 0);
  if (!meses) return;
  if (!confirm(
    `Das Archiv wird auf ${fmtVentana(meses)} erweitert.\n\n` +
    (boton.dataset.coste
      ? `Jedes zusätzliche Jahr kostet ${boton.dataset.coste}. Der Platz bleibt danach ` +
        'auf der Festplatte belegt.\n\n'
      : '') +
    'Der Abruf läuft im Hintergrund weiter; du kannst den Radar dabei benutzen. Starten?'
  )) return;

  boton.disabled = true;
  estado.textContent = 'wird gestartet …';
  let r; let d;
  try {
    r = await fetch('/api/historico', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ meses }),
    });
    d = await r.json();
  } catch {
    boton.disabled = false;
    estado.textContent = 'Der Abruf konnte nicht gestartet werden.';
    return;
  }
  if (!r.ok) {
    boton.disabled = false;
    // 409 = ya hay una ingesta tomada el cerrojo. No es un error: hay que esperarla.
    estado.textContent = r.status === 409
      ? 'Es läuft bereits ein Abruf; bitte abwarten und danach erneut versuchen.'
      : (d.error || 'Der Abruf konnte nicht gestartet werden.');
    if (r.status === 409) vigilarBusqueda();
    return;
  }
  estado.textContent = 'Der Abruf läuft. Du kannst weiterarbeiten.';
  vigilarBusqueda();
}

// --- Avisos de salud de las fuentes ----------------------------------------
//
// Se pueden cerrar, y se quedan cerrados hasta que se vuelvan a consultar datos. Eso no
// necesita ningún temporizador ni ninguna fecha de caducidad: la clave del descarte
// lleva dentro el `iniciado_en` de la ingesta que provocó el aviso, así que en cuanto
// corre una ingesta nueva la clave deja de coincidir y el aviso vuelve a salir —solo si
// sigue habiendo motivo, claro—.
//
// Hay que guardarlo fuera del DOM porque `#avisos` se reescribe entero en cada
// `cargarResumen()`, y eso ocurre cada 12 s mientras hay una búsqueda en marcha: poner
// `hidden` al cerrar duraría hasta el refresco siguiente. Es el mismo motivo que ya
// documenta el comentario de `#aviso-version` en index.html.
const AVISOS_DESCARTADOS = 'avisos-descartados';

// El orden elegido en la bandeja. Se recuerda porque el de fábrica cambió en esta
// versión —de «cierran antes» a «publicación más reciente»— y cambiar el valor por
// defecto sin dar manera de fijar el propio es cambiárselo dos veces a quien prefería
// el anterior.
const ORDEN_BANDEJA = 'orden-bandeja';

function claveAviso(f) {
  return `${f.fuente}|${f.iniciado_en || ''}`;
}

function leerDescartados() {
  try {
    const guardado = JSON.parse(localStorage.getItem(AVISOS_DESCARTADOS) || '[]');
    return new Set(Array.isArray(guardado) ? guardado : []);
  } catch {
    // Un almacén deshabilitado, lleno o con basura de otra versión no puede dejar a
    // nadie sin avisos: se empieza de cero, que como mucho cuesta volver a cerrarlos.
    return new Set();
  }
}

function guardarDescartados(claves, vigentes) {
  // Se podan las claves que ya no corresponden a ninguna ingesta con aviso: si no, la
  // lista crecería una entrada por fuente y por ingesta para siempre.
  try {
    localStorage.setItem(AVISOS_DESCARTADOS,
                         JSON.stringify([...claves].filter((c) => vigentes.has(c))));
  } catch {
    // Sin sitio donde guardarlo el aviso reaparecerá al recargar. Es mejor eso que
    // dejar de pintarlo.
  }
}

// Igual que `tarjeta()`: el `innerHTML` solo lleva el andamio, y el texto va por
// `textContent` porque `error` es el mensaje crudo de una fuente que no controlamos.
function pintarAvisos(fuentes) {
  const conAviso = (fuentes || []).filter((f) => f.aviso);
  const vigentes = new Set(conAviso.map(claveAviso));
  const descartados = leerDescartados();
  const problemas = conAviso.filter((f) => !descartados.has(claveAviso(f)));

  const av = $('avisos');
  av.textContent = '';
  av.hidden = problemas.length === 0;
  for (const f of problemas) {
    const linea = document.createElement('p');
    linea.innerHTML = '<strong></strong><span></span>' +
      '<button class="cerrar" aria-label="Hinweis schließen" title="Hinweis schließen">×</button>';
    linea.querySelector('strong').textContent = f.fuente;
    linea.querySelector('span').textContent =
      `: ${f.aviso}${f.error ? ` — ${f.error.slice(0, 180)}` : ''}`;
    linea.querySelector('.cerrar').addEventListener('click', () => {
      descartados.add(claveAviso(f));
      guardarDescartados(descartados, vigentes);
      pintarAvisos(fuentes);
    });
    av.appendChild(linea);
  }
}

async function cargarResumen() {
  const r = await fetch('/api/resumen');
  const d = await r.json();

  // Cada contador es un botón que aplica exactamente su propio filtro, y su cifra
  // sale de la misma función que alimenta la lista: al pulsarlo tienen que salir
  // esas licitaciones y no otras.
  $('kpis').innerHTML = KPIS.map(({ clave, etiqueta, clase }) =>
    `<button class="kpi ${clase || ''}" data-kpi="${clave}">
       <div class="n">${(d[clave] ?? 0).toLocaleString(LOC)}</div>
       <div class="t">${etiqueta}</div>
     </button>`
  ).join('');
  for (const b of $('kpis').querySelectorAll('button.kpi')) {
    b.addEventListener('click', () => aplicarKpi(b.dataset.kpi));
    b.classList.toggle('activo', b.dataset.kpi === kpiActivo);
  }

  // Mientras hay una búsqueda en marcha ese hueco lo ocupa el progreso, y refrescar
  // las cifras cada pocos segundos lo borraría a intervalos.
  if (!vigilando) {
    $('ultima-busqueda').textContent = d.ultima_busqueda
      ? 'letzte Suche: ' + fmtFechaHora(d.ultima_busqueda)
      : 'noch nichts gesucht';
  }

  const sel = $('perfil');
  if (sel.options.length <= 1) {
    for (const p of d.por_perfil) {
      sel.add(new Option(`${p.perfil} (${p.total})`, p.perfil));
    }
  }
  const selC = $('bundesland');
  if (selC.options.length <= 1) {
    for (const c of d.bundesland) selC.add(new Option(`${c.bundesland} (${c.total})`, c.bundesland));
  }
  // El de la Analítica es otro control, no el mismo: el de arriba vive dentro de
  // #filtros, que se oculta en cualquier vista que no sea la bandeja.
  const selA = $('analitica-perfil');
  if (selA.options.length <= 1) {
    for (const p of d.por_perfil) selA.add(new Option(p.perfil, p.perfil));
  }

  // Salud de las fuentes: una fuente rota y una fuente sin novedades se ven
  // igual si no se avisa explícitamente.
  pintarAvisos(d.fuentes);
  pintarCobertura(d.cobertura);
  pintarHistorico(d.historico);

  // Pestaña de novedades: solo aparece si hay algo nuevo desde la última visita.
  const tabN = $('tab-novedades');
  if (d.novedades > 0) {
    tabN.hidden = false;
    tabN.textContent = `${d.novedades} neu`;
  } else if (!soloNovedades) {
    tabN.hidden = true;
  }

  $('salud').textContent = (d.fuentes || []).length
    ? 'Letzter Abruf — ' + d.fuentes.map((f) =>
        `${f.fuente}: ${f.ok ? `${f.vistos.toLocaleString(LOC)} geprüft, ${f.nuevos} neu` : 'Fehler'}`
      ).join(' · ')
    : 'Noch kein Abruf. Starte: python3 radar.py ingest';
}

// --- Lista -----------------------------------------------------------------

// Un `<svg><use>` al sprite de `index.html`. Se crea como elemento y no como cadena
// porque todo lo que se monta en una tarjeta va por este camino: en cuanto una píldora
// se construye con `innerHTML`, el texto del pliego que lleva dentro se interpreta.
function icono(nombre) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'ico');
  svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', '#' + nombre);
  svg.appendChild(use);
  return svg;
}

// Una píldora. `texto` SIEMPRE por `textContent`: viene de los pliegos.
//
// Antes se montaban con plantillas de cadena, y el comentario de aquí abajo decía —con
// razón— que el objeto y el órgano iban por `textContent`... pero el adjudicatario, la
// comunidad, el procedimiento y la fuente se interpolaban en el `innerHTML`. Son campos
// que llegan de la fuente sin que los controlemos; el riesgo práctico es bajo, pero la regla
// que el fichero se da a sí mismo no admite excepciones que nadie ha decidido.
function pildora(texto, { clase = '', ico = null, titulo = '' } = {}) {
  const el = document.createElement('span');
  el.className = clase ? `pill ${clase}` : 'pill';
  if (ico) el.appendChild(icono(ico));
  const t = document.createElement('span');
  t.textContent = texto;
  el.appendChild(t);
  if (titulo) el.title = titulo;
  return el;
}

function tarjeta(it) {
  const d = it.dias_restantes;

  const el = document.createElement('article');
  el.className = `tarjeta rev-${it.estado_revision}`;
  el.dataset.id = it.id;
  // El andamio, sin un solo dato dentro: los datos se meten abajo, uno a uno.
  el.innerHTML = `
    <div class="principal">
      <h3></h3>
      <div class="organo"></div>
    </div>
    <div class="derecha"></div>
    <div class="meta"></div>`;

  el.querySelector('h3').textContent = it.objeto || '(ohne Bezeichnung)';
  const organo = el.querySelector('.organo');
  if (it.organo) {
    organo.appendChild(icono('ico-organo'));
    const n = document.createElement('span');
    n.textContent = it.organo;
    organo.appendChild(n);
  }

  // --- La columna de la derecha ---------------------------------------------
  //
  // La cifra grande sigue siendo los días al cierre, que es la que se decide mirando:
  // la antigüedad de la publicación ordena la lista, pero no dice qué hacer. Lo que se
  // añade debajo es esa antigüedad, en pequeño, porque desde que la bandeja abre por
  // «publicación más reciente» hace falta poder ver por qué una ficha está donde está.
  const derecha = el.querySelector('.derecha');
  const dias = document.createElement('div');
  if (d === null || d === undefined) {
    dias.className = 'dias';
    dias.innerHTML = '<small>ohne Frist</small>';
  } else {
    dias.className = d < 0 ? 'dias vencido' : d <= 7 ? 'dias pronto' : 'dias';
    const n = document.createElement('span');
    n.textContent = String(d);
    dias.appendChild(n);
    const u = document.createElement('small');
    u.textContent = 'Tage';
    dias.appendChild(u);
  }
  derecha.appendChild(dias);

  const desde = it.dias_desde_publicacion;
  if (desde !== null && desde !== undefined) {
    const p = document.createElement('div');
    p.className = it.es_nueva ? 'publicada recien' : 'publicada';
    p.textContent = desde === 0 ? 'hoy'
      : desde === 1 ? 'vor 1 Tag'
      : `vor ${desde.toLocaleString(LOC)} Tagen`;
    p.title = 'erste Veröffentlichung des Vorgangs';
    derecha.appendChild(p);
  }

  // --- Las píldoras ---------------------------------------------------------
  //
  // Dos rangos, y el orden importa porque es el orden en que se leen. Primero las que
  // deciden —si es nueva, de qué perfil es, cuánto vale, cuándo cierra—, después las
  // descriptivas, que se reconocen de un vistazo y no se leen.
  const meta = el.querySelector('.meta');

  // Va la primera porque es lo único de la fila que caduca solo. `es_nueva` lo decide
  // el servidor sobre la PRIMERA publicación del expediente: una adjudicación de ayer
  // sobre un pliego de junio no es una oportunidad nueva, y marcarla como tal manda a
  // alguien a un contrato ya cerrado.
  if (it.es_nueva) {
    const cuando = desde === 0 ? 'heute veröffentlicht'
      : `vor ${desde} ${desde === 1 ? 'Tag' : 'Tagen'} veröffentlicht`;
    meta.appendChild(pildora('Neu', { clase: 'nueva', ico: 'ico-destello', titulo: cuando }));
  }
  // `perfil` puede traer varios separados por coma: una licitación de protección de
  // correo con formación casa con dos perfiles y antes salía duplicada en la lista.
  for (const p of (it.perfil || '').split(',').filter(Boolean)) {
    meta.appendChild(pildora(p.trim(), { clase: 'perfil' }));
  }
  meta.appendChild(pildora(fmtImporte(it.importe_referencia), { clase: 'importe' }));
  if (it.fecha_limite_presentacion) {
    const c = d === null ? '' : d < 0 ? 'plazo-vencido' : d <= 7 ? 'plazo-pronto' : 'plazo-ok';
    meta.appendChild(pildora(`Frist ${fmtFecha(it.fecha_limite_presentacion)}`,
                             { clase: c, ico: 'ico-reloj' }));
  }
  if (it.adjudicatario) {
    meta.appendChild(pildora(`an ${it.adjudicatario.slice(0, 38)}`, { clase: 'incumbente' }));
  }
  // Varios anuncios del mismo expediente colapsados en una fila.
  if ((it.anuncios || 1) > 1) {
    meta.appendChild(pildora(`${it.anuncios} Bekanntmachungen`, { clase: 'anuncios' }));
  }
  if (it.bundesland) meta.appendChild(pildora(it.bundesland));
  if (it.procedimiento) meta.appendChild(pildora(it.procedimiento));
  meta.appendChild(pildora(it.fuente));
  if (it.estado_revision !== 'nuevo') meta.appendChild(pildora(it.estado_revision));

  el.addEventListener('click', () => abrirPanel(it.id));
  return el;
}

async function cargarLista(reset = true) {
  // Sin `reset` esto es «Cargar más»: añade al final, así que no se vacía nada ni se
  // avisa de carga; lo que hay en pantalla sigue siendo válido.
  const carga = reset ? empezarCarga('lista') : null;
  if (reset) offset = 0;

  let d;
  try {
    const r = await fetch('/api/bandeja?' + query({ limite: POR_PAGINA, offset }));
    d = await r.json();
  } catch {
    if (!carga || carga.terminar()) $('contador').textContent = 'Die Liste konnte nicht geladen werden.';
    return;
  }
  if (carga && !carga.terminar()) return;

  if (d.error) {
    $('contador').textContent = d.error;
    $('mas').hidden = true;
    return;
  }

  ultimoTotal = d.total;
  const frag = document.createDocumentFragment();
  for (const it of d.items) frag.appendChild(tarjeta(it));
  $('lista').appendChild(frag);

  // "57 de 668 coincidencias" en lugar de "57 licitaciones": responde solo a la
  // pregunta de por qué no salen todas.
  const totalTxt = d.total.toLocaleString(LOC);
  $('contador').textContent = d.total === d.total_sin_filtros
    ? `${totalTxt} ${d.total === 1 ? 'coincidencia' : 'coincidencias'}`
    : `${totalTxt} von ${d.total_sin_filtros.toLocaleString(LOC)} Treffern`;

  const activos = describirFiltros();
  const fa = $('filtros-activos');
  if (activos.length) {
    fa.hidden = false;
    fa.textContent = 'Gefiltert nach: ' + activos.join(' · ');
    const btn = document.createElement('button');
    btn.className = 'quitar';
    btn.textContent = 'alle zeigen';
    btn.addEventListener('click', limpiarFiltros);
    fa.appendChild(btn);
  } else {
    fa.hidden = true;
  }

  $('vacio').hidden = d.total !== 0;
  if (d.total === 0) {
    $('vacio').textContent = activos.length
      ? 'Nichts mit diesen Filtern.'
      : 'Keine Treffer. Klick auf „Jetzt suchen“, um Ausschreibungen zu laden.';
  }
  offset += d.items.length;
  $('mas').hidden = offset >= d.total;
  $('exportar').href = '/api/export.csv?' + query();
}

// --- Buscar ahora ----------------------------------------------------------

let vigilando = null;
// Si hay una ingesta corriendo AHORA. No vale mirar `vigilando`: el temporizador arranca
// al abrir la página para comprobarlo y tarda un tic y medio en saber la respuesta, así
// que durante ese rato «hay un vigilante» y «hay una descarga» no son lo mismo.
let hayBusqueda = false;
let seVioEnMarcha = false;
let ticsVigilando = 0;

// 8 × 1,5 s: las cifras de la cabecera se refrescan cada 12 segundos mientras carga.
const TICS_POR_REFRESCO = 8;

function fmtBytes(n) {
  const mb = (n || 0) / (1024 * 1024);
  return mb >= 1024
    ? (mb / 1024).toFixed(1).replace('.', ',') + ' GB'
    : Math.round(mb) + ' MB';
}

// La misma cadena que `_ritmo()` en progreso.py, para que la terminal y la pantalla no
// digan cifras distintas. `fmtBytes` no sirve aquí: redondea a MB enteros y estas
// descargas van por debajo de 2 MB/s, así que saldrían todas como «1 MB».
function fmtVelocidad(bytesPorS) {
  return ((bytesPorS || 0) / (1024 * 1024)).toFixed(1).replace('.', ',') + ' MB/s';
}

// Mismo formato que la línea de la terminal: 45s, 12m, 1h 39m.
function fmtDuracion(segundos) {
  const s = Math.round(segundos || 0);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`;
}

// Aviso de que el histórico está entrando por detrás. Solo sale en la carga inicial:
// `detalle.etapas` es 0 en una búsqueda normal, que ya se cuenta en la cabecera y no
// merece ocupar media pantalla.
function pintarCarga(det) {
  const caja = $('carga');
  if (!det || !det.etapas) {
    caja.hidden = true;
    return;
  }
  caja.hidden = false;

  $('carga-titulo').textContent =
    `Historie wird geladen · Schritt ${det.etapa} von ${det.etapas}` +
    (det.etiqueta ? ` · ${det.etiqueta}` : '');
  // Qué cubre esta etapa y lo que va a tardar. Decir de antemano que son dos horas es
  // lo que evita que una espera normal se lea como un cuelgue.
  $('carga-detalle').textContent = [
    det.detalle_etapa ? det.detalle_etapa.charAt(0).toUpperCase() + det.detalle_etapa.slice(1) + '.' : '',
    det.coste ? `Dieser Schritt dauert ${det.coste}.` : '',
    'Du kannst schon arbeiten: die Ausschreibungen erscheinen nach und nach.',
  ].filter(Boolean).join(' ');
  // La frase la compone el indicador en Python, para que la terminal y esto cuenten lo
  // mismo. Aquí solo se le añade el reloj, que es lo que demuestra que avanza.
  $('carga-latido').textContent =
    (det.frase || det.resumen || '') +
    (det.segundos ? ` Läuft seit ${fmtDuracion(det.segundos)}.` : '');

  $('carga-etapa-txt').textContent = `Schritt ${det.etapa} von ${det.etapas}`;
  const etapa = $('carga-etapa');
  etapa.max = det.etapas;
  etapa.value = Math.max(0, det.etapa - 1);

  const barra = $('carga-tarea');
  const txt = $('carga-tarea-txt');
  const quien = det.titulo || det.fuente || 'preparando';
  const descargando = (det.fase || '').startsWith('descargando');
  if (descargando && det.bytes_total > 0) {
    barra.max = det.bytes_total;
    barra.value = det.bytes;
    txt.textContent = `lädt ${fmtBytes(det.bytes)} von ${fmtBytes(det.bytes_total)}`;
  } else if (descargando) {
    // La descarga manda sobre el contador de ficheros aunque este traiga números: son
    // los del ZIP del año anterior, y dejarlos pintados es lo que hacía parecer que la
    // aplicación se había quedado clavada al 100%.
    barra.removeAttribute('value');
    txt.textContent =
      `descargando ${fmtBytes(det.bytes)}` +
      (det.bytes_por_s ? ` · ${fmtVelocidad(det.bytes_por_s)}` : '') +
      ' — der Server nennt die Größe nicht, daher ohne Prozentangabe';
  } else if (det.subtareas > 0) {
    barra.max = det.subtareas;
    barra.value = det.subtarea;
    txt.textContent = `Datei ${det.subtarea} von ${det.subtareas} im ZIP`;
  } else {
    // Sin total conocido la barra se deja indeterminada: es más honesto que
    // inventarse un porcentaje que no significa nada.
    barra.removeAttribute('value');
    txt.textContent = quien;
  }
}

async function lanzarBusqueda(opciones = {}) {
  const btn = $('buscar-ahora');
  const r = await fetch('/api/buscar', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(opciones),
  });
  const d = await r.json();
  if (!r.ok) {
    // 409 = ya hay una en marcha; no es un error, solo hay que esperarla. Pasa a
    // diario durante la carga inicial, que dura horas y tiene el cerrojo tomado.
    $('ultima-busqueda').textContent = r.status === 409
      ? 'wird bereits geladen; bitte abwarten'
      : (d.error || 'Die Suche konnte nicht gestartet werden.');
    if (r.status === 409) vigilarBusqueda();
    return;
  }
  btn.disabled = true;
  btn.textContent = 'Wird gesucht …';
  vigilarBusqueda();
}

function vigilarBusqueda() {
  if (vigilando) return;
  const btn = $('buscar-ahora');
  seVioEnMarcha = false;
  ticsVigilando = 0;

  vigilando = setInterval(async () => {
    let d;
    try {
      d = await (await fetch('/api/busqueda-estado')).json();
    } catch {
      return;  // el servidor puede tardar un instante; se reintenta al siguiente tic
    }
    if (d.en_marcha) {
      seVioEnMarcha = true;
      hayBusqueda = true;
      const det = d.detalle;
      btn.disabled = true;
      btn.textContent = det && det.etapas
        ? `Erstbefüllung (${det.etapa}/${det.etapas})`
        : 'Wird gesucht …';
      pintarCarga(det);
      // Durante la carga inicial toda la narración vive en el bloque de abajo. Aquí
      // solo el estado corto: antes se pintaba la última línea del log recortada a 90
      // caracteres, que decía lo mismo en jerga y cortada a mitad de palabra.
      if (det && det.etapas) {
        $('ultima-busqueda').textContent =
          `Erstbefüllung läuft · Schritt ${det.etapa} von ${det.etapas}` +
          (det.segundos ? ` · ${fmtDuracion(det.segundos)}` : '');
      } else {
        // '[match]' es un marcador que pone radar.py, no una palabra traducible.
        const ultima = (d.progreso || []).filter((l) => !l.startsWith('[match]')).pop();
        $('ultima-busqueda').textContent = ultima ? ultima.slice(0, 90) : 'wird geladen …';
      }

      // Solo los contadores, y no la lista: cada etapa de la carga inicial reevalúa
      // los perfiles, así que las cifras van subiendo y es eso lo que hay que poder
      // ver sin recargar. Recargar la lista movería el sitio por donde se va leyendo.
      if (++ticsVigilando % TICS_POR_REFRESCO === 0) cargarResumen();
      return;
    }
    clearInterval(vigilando);
    vigilando = null;
    hayBusqueda = false;
    pintarCarga(null);
    btn.disabled = false;
    btn.textContent = 'Jetzt suchen';
    if (seVioEnMarcha) {
      await cargarResumen();
      await cargarLista();
    }
  }, 1500);
}

// --- Vencimientos ----------------------------------------------------------

let mesesVencimiento = 6;

async function cargarVencimientos() {
  const meses = mesesVencimiento;
  const carga = empezarCarga('lista-vencimientos');
  // Los botones de ventana y el total se borran también: son del plazo anterior y
  // dejarlos mientras carga el nuevo es peor que no mostrar nada.
  $('ventanas').innerHTML = '';
  $('total-ventana').textContent = '';

  const cont = $('lista-vencimientos');
  let d;
  try {
    const r = await fetch('/api/vencimientos?meses=' + meses);
    d = await r.json();
  } catch {
    if (carga.terminar()) {
      cont.innerHTML = '<p class="vacio">Die Vertragsenden konnten nicht geladen werden.</p>';
    }
    return;
  }
  if (!carga.terminar()) return;

  // Botones de ventana con su recuento e importe, para comparar de un vistazo.
  $('ventanas').innerHTML = (d.por_ventana || []).map((v) =>
    `<button class="ventana ${v.meses === meses ? 'activa' : ''}" data-meses="${v.meses}">
       <div class="n">${v.total.toLocaleString(LOC)}</div>
       <div class="t">${v.meses} Monate</div>
       <div class="imp">${fmtImporte(v.importe)}</div>
     </button>`
  ).join('');
  for (const b of $('ventanas').querySelectorAll('.ventana')) {
    b.addEventListener('click', () => {
      mesesVencimiento = Number(b.dataset.meses);
      cargarVencimientos();
    });
  }

  const plural = d.total === 1 ? 'Vertrag läuft aus' : 'Verträge laufen aus';
  $('total-ventana').innerHTML =
    `<strong>${d.total.toLocaleString(LOC)}</strong> ${plural} in den nächsten ` +
    `${meses} Monaten, zusammen <strong>${fmtImporte(d.importe_total)}</strong>.`;

  if (!d.items.length) {
    cont.innerHTML = `<p class="vacio">In den nächsten ${meses} Monaten läuft nichts aus.
      Dafür braucht es die Historie der Zuschläge:
      <code>python3 radar.py ingest --primera-carga</code></p>`;
    return;
  }

  for (const it of d.items) {
    const el = document.createElement('article');
    el.className = 'tarjeta';
    el.dataset.id = it.id;
    const meses_txt = it.duracion_meses
      ? `Laufzeit ${Math.round(it.duracion_meses)} Monate` : '';
    el.innerHTML = `
      <div>
        <h3></h3>
        <div class="organo"></div>
        <div class="meta">
          <span class="pill incumbente"></span>
          <span class="pill importe">${fmtImporte(it.importe)}</span>
          <span class="pill">Ende ${fmtFecha(it.fecha_fin_prevista)}</span>
          ${meses_txt ? `<span class="pill">${meses_txt}</span>` : ''}
          ${it.bundesland ? `<span class="pill">${it.bundesland}</span>` : ''}
        </div>
      </div>
      <div class="derecha">
        <div class="dias ${it.dias_para_vencer <= 90 ? 'pronto' : ''}">${it.dias_para_vencer}<small>Tage</small></div>
      </div>`;
    el.querySelector('h3').textContent = it.objeto || '(ohne Bezeichnung)';
    el.querySelector('.organo').textContent = it.organo || '';
    el.querySelector('.incumbente').textContent =
      'Bestandsanbieter: ' + (it.adjudicatario || 'nicht veröffentlicht');
    el.addEventListener('click', () => abrirPanel(it.id));
    cont.appendChild(el);
  }
}

// --- Adjudicatarios --------------------------------------------------------

async function cargarAdjudicatarios() {
  const carga = empezarCarga('lista-adjudicatarios');
  const cont = $('lista-adjudicatarios');
  let d;
  try {
    const r = await fetch('/api/adjudicatarios?limite=30');
    d = await r.json();
  } catch {
    if (carga.terminar()) {
      cont.innerHTML = '<p class="vacio">Die Auftragnehmer konnten nicht geladen werden.</p>';
    }
    return;
  }
  if (!carga.terminar()) return;

  if (!d.items.length) {
    cont.innerHTML = `<p class="vacio">Noch keine Zuschläge in der Datenbank.
      Versuch: <code>python3 radar.py ingest --primera-carga</code></p>`;
    return;
  }

  for (const e of d.items) {
    const el = document.createElement('article');
    el.className = 'tarjeta fila-empresa';
    el.innerHTML = `
      <div>
        <h3></h3>
        <div class="organo">${e.organos} ${e.organos === 1 ? 'Vergabestelle' : 'Vergabestellen'}</div>
      </div>
      <div class="cifras">
        <span><b>${e.contratos}</b> Aufträge</span>
        <span><b>${fmtImporte(e.importe)}</b></span>
      </div>`;
    el.querySelector('h3').textContent = e.empresa;
    el.addEventListener('click', () => alternarContratos(el, e.empresa));
    cont.appendChild(el);
  }
}

async function alternarContratos(el, empresa) {
  const previo = el.querySelector('.contratos-empresa');
  if (previo) { previo.remove(); return; }
  const r = await fetch('/api/contratos-empresa?empresa=' + encodeURIComponent(empresa));
  const d = await r.json();
  const ul = document.createElement('ul');
  ul.className = 'contratos-empresa';
  for (const c of d.items) {
    const li = document.createElement('li');
    const fecha = c.fecha_adjudicacion ? fmtFecha(c.fecha_adjudicacion) : 'o. D.';
    li.textContent = `${fecha} · ${fmtImporte(c.importe)} · ${c.organo || ''} — `;
    const a = document.createElement('a');
    a.href = c.url_detalle || '#';
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.textContent = (c.objeto || '(ohne Bezeichnung)').slice(0, 90);
    li.appendChild(a);
    ul.appendChild(li);
  }
  el.appendChild(ul);
}

// --- Términos de búsqueda --------------------------------------------------

// Las listas de términos se editan como texto, una por línea: es lo más simple sin
// saber JSON, y el orden no importa.
const CAMPOS_TERMINOS = [
  ['starke_begriffe', 'Eindeutige Begriffe',
   'reichen allein für einen Treffer (Phishing, DMARC, Awareness-Plattform …)'],
  ['schwache_begriffe', 'Mehrdeutige Begriffe',
   'zählen nur zusammen mit dem Kontext unten (Schulung, Sensibilisier …)'],
  ['erforderlicher_kontext', 'Erforderlicher Kontext',
   'was bestätigt, dass es um Sicherheit geht (IT-Sicherheit, Ransomware …)'],
  ['ausschluss', 'Ausschlüsse',
   'stechen alles andere (Cybermobbing, Wachdienst …)'],
  ['abfrage_begriffe', 'Was an die Quellen geschickt wird',
   'nur für Quellen, die serverseitig filtern; keine allgemeinen Wörter'],
  ['cpv_praefixe', 'CPV-Codes',
   'geben Punkte, nehmen aber nie allein auf'],
];

let perfilesOriginales = null;

async function cargarAjustes() {
  const carga = empezarCarga('lista-perfiles');
  let d;
  try {
    d = await (await fetch('/api/perfiles')).json();
  } catch {
    if (carga.terminar()) {
      $('lista-perfiles').innerHTML =
        '<p class="vacio">Die Suchbegriffe konnten nicht geladen werden.</p>';
    }
    return;
  }
  if (!carga.terminar()) return;

  perfilesOriginales = JSON.parse(JSON.stringify(d.profile));

  $('consejos').innerHTML = '<ul>' + [
    '<b>Nutze Wortstämme, keine ganzen Wörter.</b> „sensibilisier“ deckt Sensibilisierung, sensibilisieren und Sensibilisierungsmaßnahme ab.',
    '<b>Lass die Tippfehler drin.</b> „phising“ mit einem s steht so in veröffentlichten Unterlagen. Und Englisch bleibt englisch: „Awareness“ und „Phishing“ werden nicht übersetzt.',
    '<b>Deutsch schreibt zusammen, und der Treffer steckt hinten.</b> Ein Begriff greift nur am Wortanfang: „schulung“ findet „Schulungskonzept“ und – dank Bindestrich – „Awareness-Schulung“, aber nicht „Mitarbeiterschulung“. Solche Komposita einzeln eintragen.',
    '<b>Leerzeichen zählen.</b> „bsi “ mit Leerzeichen am Ende sucht das Kürzel allein. Umlaute und ß sind egal: „Maßnahmen“ = „Massnahmen“, „Prüfung“ = „Prufung“.',
    '<b>Klick „Vorschau“, bevor du speicherst.</b> Sie sagt dir, wie viele Ausschreibungen mit den neuen Begriffen dazukommen und wegfallen.',
  ].map((t) => `<li>${t}</li>`).join('') + '</ul>';

  const cont = $('lista-perfiles');
  cont.innerHTML = '';
  d.profile.forEach((p, i) => {
    const caja = document.createElement('section');
    caja.className = 'perfil-caja';
    caja.dataset.indice = i;
    caja.innerHTML = `
      <header>
        <h3></h3>
        <label class="check"><input type="checkbox" data-campo="aktiv" ${p.aktiv ? 'checked' : ''}> aktiv</label>
        <span class="campo-num">Mindestwert
          <input type="number" data-campo="mindestbetrag" min="0" step="1000"
                 value="${p.mindestbetrag ?? ''}" placeholder="kein Mindestwert"> €</span>
      </header>
      <div class="campos">
        ${CAMPOS_TERMINOS.map(([clave, titulo, ayuda]) => `
          <div class="campo-term">
            <label><b>${titulo}</b><br>${ayuda}</label>
            <textarea data-campo="${clave}" spellcheck="false">${(p[clave] || []).join('\n')}</textarea>
          </div>`).join('')}
      </div>`;
    caja.querySelector('h3').textContent = p.name;
    cont.appendChild(caja);
  });
  $('previsualizacion').hidden = true;
  $('ajustes-aviso').textContent = '';
}

function leerAjustes() {
  // Los espacios de los extremos NO se recortan: hay términos que los llevan a
  // propósito para casar una palabra suelta. «ens » con espacio busca la sigla ENS;
  // recortado a «ens» casa dentro de "ensayo" o "enseñanza" y mete licitaciones de más.
  // (En "defensa" o "bienes" no, porque el matcher ancla a principio de palabra: ver
  // `patron()`.) Solo se descartan las líneas vacías y el retorno de carro.
  const lineas = (t) => t
    .split('\n')
    .map((s) => s.replace(/\r/g, ''))
    .filter((s) => s.trim() !== '');
  return [...$('lista-perfiles').querySelectorAll('.perfil-caja')].map((caja) => {
    const original = perfilesOriginales[Number(caja.dataset.indice)];
    // Se parte del original para no perder campos que la pantalla no muestra
    // (fuentes, bundesland) en lugar de reescribir el perfil desde cero.
    const p = { ...original };
    p.aktiv = caja.querySelector('[data-campo="aktiv"]').checked;
    const imp = caja.querySelector('[data-campo="mindestbetrag"]').value.trim();
    p.mindestbetrag = imp === '' ? null : Number(imp);
    for (const [clave] of CAMPOS_TERMINOS) {
      p[clave] = lineas(caja.querySelector(`[data-campo="${clave}"]`).value);
    }
    return p;
  });
}

function avisoDeAlcance(perfiles) {
  // Las fuentes que filtran en su servidor —hoy solo TED— no devuelven nada nuevo con
  // un término nuevo en abfrage_begriffe hasta que se les vuelve a preguntar.
  const cambiado = perfiles.some((p, i) => {
    const o = perfilesOriginales[i] || {};
    return JSON.stringify(p.abfrage_begriffe || []) !== JSON.stringify(o.abfrage_begriffe || [])
        || JSON.stringify(p.cpv_praefixe || []) !== JSON.stringify(o.cpv_praefixe || []);
  });
  return cambiado;
}

async function previsualizarAjustes() {
  const perfiles = leerAjustes();
  const caja = $('previsualizacion');
  const btn = $('previsualizar');
  // Recorrer todo lo descargado tarda un par de segundos: sin este aviso parece
  // que el botón no ha hecho nada.
  caja.hidden = false;
  caja.textContent = 'Wird über alle geladenen Daten berechnet …';
  btn.disabled = true;
  let r, d;
  try {
    r = await fetch('/api/perfiles', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile: perfiles, previsualizar: true }),
    });
    d = await r.json();
  } finally {
    btn.disabled = false;
  }
  if (!r.ok) {
    caja.innerHTML = `<strong>Speichern nicht möglich:</strong> ${d.error}`;
    return;
  }
  const lista = (titulo, items) => items.length
    ? `<h4>${titulo}</h4><ul>${items.map((x) =>
        `<li>${(x.objeto || '').slice(0, 95)} — ${x.organo || ''}</li>`).join('')}</ul>`
    : '';
  const avisos = (d.avisos || []).length
    ? `<h4>Zu beachten</h4><ul>${
        d.avisos.map((a) => `<li>${a}</li>`).join('')}</ul>`
    : '';
  caja.innerHTML =
    `Statt <strong>${d.antes.toLocaleString(LOC)}</strong> wären es ` +
    `<strong>${d.despues.toLocaleString(LOC)}</strong> Treffer ` +
    `(${d.entran} kommen dazu, ${d.salen} fallen weg).` +
    avisos +
    lista('Beispiele für neu aufgenommene', d.muestra_entran) +
    lista('Beispiele für wegfallende', d.muestra_salen);
}

async function guardarAjustes() {
  const perfiles = leerAjustes();
  const hayQueRebuscar = avisoDeAlcance(perfiles);
  const r = await fetch('/api/perfiles', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ profile: perfiles }),
  });
  const d = await r.json();
  if (!r.ok) {
    $('ajustes-aviso').textContent = d.error;
    return;
  }
  $('ajustes-aviso').textContent =
    `Gespeichert · ${d.coincidencias.toLocaleString(LOC)} Treffer`;
  perfilesOriginales = JSON.parse(JSON.stringify(perfiles));
  await cargarResumen();

  if (hayQueRebuscar) {
    const caja = $('previsualizacion');
    caja.hidden = false;
    caja.innerHTML =
      '<strong>Du hast geändert, was an die Quellen geschickt wird.</strong> Quellen, die ' +
      'serverseitig filtern, liefern mit den neuen Begriffen nichts Neues, ' +
      'bis man sie erneut abfragt. Das dauert länger als eine normale Suche.';
    const btn = document.createElement('button');
    btn.className = 'boton-pri';
    btn.style.marginTop = '10px';
    btn.textContent = 'Quellen erneut abfragen';
    btn.addEventListener('click', () => {
      caja.hidden = true;
      mostrarVista('bandeja');
      lanzarBusqueda({ reiniciar_cursor: true, dias: 365 });
    });
    caja.appendChild(btn);
  }
}

// --- Cambio de vista -------------------------------------------------------

// El panel y su fondo se abren y se cierran juntos. Antes había tres sitios distintos
// poniendo `$('panel').hidden` a mano, y con el fondo serían seis.
function abrirCajon() {
  $('panel-fondo').hidden = false;
  $('panel').hidden = false;
}

function cerrarCajon() {
  $('panel').hidden = true;
  $('panel-fondo').hidden = true;
}

function mostrarVista(vista) {
  for (const v of ['bandeja', 'vencimientos', 'adjudicatarios', 'analitica', 'ajustes']) {
    $('vista-' + v).hidden = v !== vista;
  }
  $('filtros').hidden = vista !== 'bandeja';
  for (const b of document.querySelectorAll('.tab[data-vista]')) {
    const activa = b.dataset.vista === vista;
    b.classList.toggle('activo', activa);
    // La clase es para el ojo; `aria-selected` para quien no lo usa. Alternar solo la
    // primera dejaba a un lector de pantalla seis pestañas sin ninguna seleccionada.
    b.setAttribute('aria-selected', activa ? 'true' : 'false');
  }
  if (vista === 'vencimientos') cargarVencimientos();
  if (vista === 'adjudicatarios') cargarAdjudicatarios();
  if (vista === 'analitica') cargarAnalitica();
  if (vista === 'ajustes') cargarAjustes();
}

// --- Panel de detalle ------------------------------------------------------

// Estado, etiqueta e icono. El icono importa aquí más que en ningún otro sitio: son
// cuatro botones idénticos en fila y el único que se pulsa por error es el de descartar.
const ESTADOS = [
  ['nuevo', 'Ungeprüft', 'ico-punto'],
  ['siguiendo', 'Beobachten', 'ico-ojo'],
  ['presentada', 'Abgegeben', 'ico-visto'],
  ['descartado', 'Verwerfen', 'ico-cruz'],
];

// [clave, etiqueta]. La CLAVE tiene que coincidir con db.MOTIVOS_DESCARTE —el servidor
// rechaza cualquier otra— y por eso no se traduce: nadie la ve. Solo cambia la etiqueta.
const MOTIVOS = [
  ['fuera de nicho', 'Nicht unser Thema'],
  ['importe bajo', 'Auftragswert zu gering'],
  ['incumbente atado', 'Bestandsanbieter gesetzt'],
  ['fuera de plazo', 'Frist verpasst'],
  ['ya presentada por otro', 'Von jemand anderem abgegeben'],
  ['otro', 'Sonstiges'],
];

function pedirMotivoDescarte() {
  return new Promise((resolve) => {
    const cont = $('p-acciones');
    const caja = document.createElement('div');
    caja.className = 'acciones';
    caja.style.marginTop = '8px';
    caja.innerHTML = '<span style="font-size:12px;color:var(--texto-sec);width:100%">Warum verwerfen?</span>' +
      MOTIVOS.map(([m, etq]) => `<button class="boton-sec" data-m="${m}">${etq}</button>`).join('') +
      '<button class="boton-sec" data-m="">abbrechen</button>';
    cont.after(caja);
    for (const b of caja.querySelectorAll('button')) {
      b.addEventListener('click', () => {
        caja.remove();
        resolve(b.dataset.m || null);
      });
    }
  });
}

async function abrirPanel(id) {
  const r = await fetch('/api/licitacion/' + id);
  const d = await r.json();
  if (d.error) return;

  // [clave ASCII, etiqueta, valor]. La clave va en `data-c` y la etiqueta se pinta:
  // antes eran la misma cosa, así que la etiqueta hacía de selector CSS y bastaba una
  // comilla en una traducción para romper la ficha entera.
  const campos = [
    ['organo', 'Vergabestelle', d.organo],
    ['expediente', 'Vergabenummer', d.expediente],
    ['importe', 'Auftragswert', fmtImporte(d.importe_referencia)],
    ['frist', 'Angebotsfrist', fmtFecha(d.fecha_limite_presentacion)],
    ['publicada', 'Veröffentlicht', fmtFecha(d.fecha_publicacion)],
    ['estado', 'Status', ETIKETTEN_STATUS[d.estado] || d.estado],
    ['procedimiento', 'Verfahrensart', d.procedimiento],
    ['tipo', 'Auftragsart', d.tipo_contrato],
    ['lugar', 'Erfüllungsort', [d.lugar, d.bundesland].filter(Boolean).join(' · ')],
    ['cpv', 'CPV', (d.cpv || []).join(', ')],
    ['fuente', 'Quelle', d.fuente],
    ['adjudicatario', 'Auftragnehmer', d.adjudicatario],
  ].filter(([, , v]) => v);

  const cont = $('panel-contenido');
  cont.innerHTML = `
    <h2></h2>
    <dl>${campos.map(([k, etq]) => `<div class="campo"><dt>${etq}</dt><dd data-c="${k}"></dd></div>`).join('')}</dl>
    <h4>Warum dieser Treffer</h4>
    <div class="motivo" id="p-motivo"></div>
    <h4>Bearbeitungsstatus</h4>
    <div class="acciones" id="p-acciones"></div>
    <textarea id="p-notas" placeholder="Notizen: mit wem sprechen, was fragen, was entschieden …"></textarea>
    <div><button class="boton-sec" id="p-guardar">Notizen speichern</button><span id="p-ok" class="guardado"></span></div>
    <h4>Links</h4>
    <div class="enlaces" id="p-enlaces"></div>
    <h4>Verlauf</h4>
    <ul class="hist" id="p-hist"></ul>`;

  cont.querySelector('h2').textContent = d.objeto || '(ohne Bezeichnung)';
  for (const [k, , v] of campos) {
    cont.querySelector(`dd[data-c="${k}"]`).textContent = v;
  }
  $('p-motivo').textContent = d.motivo
    ? (d.perfil ? `${d.perfil.split(',').join(' · ')} — ${d.motivo}` : d.motivo)
    : 'Diese Ausschreibung ist in der Datenbank, passt aber zu keinem aktiven Suchprofil.';
  if (d.descripcion) {
    const p = document.createElement('p');
    p.className = 'descripcion';
    p.textContent = d.descripcion;
    $('p-motivo').after(p);
  }

  // Acciones de triaje
  const actual = d.estado_revision || 'nuevo';
  $('p-acciones').innerHTML = ESTADOS.map(([v, t, ico]) =>
    `<button class="boton-sec ${v === actual ? 'activo' : ''}" data-e="${v}">` +
    `<svg class="ico" aria-hidden="true"><use href="#${ico}"/></svg>${t}</button>`
  ).join('');
  for (const b of $('p-acciones').querySelectorAll('button')) {
    b.addEventListener('click', async () => {
      const cambios = { estado: b.dataset.e };
      // Al descartar se pregunta por qué: es lo que permite afinar los perfiles
      // con datos en vez de a ojo.
      if (b.dataset.e === 'descartado') {
        const motivo = await pedirMotivoDescarte();
        if (motivo === null) return;
        cambios.motivo_descarte = motivo;
      }
      await guardar(id, cambios);
      abrirPanel(id);
      cargarLista();
      cargarResumen();
    });
  }

  $('p-notas').value = d.notas || '';
  $('p-guardar').addEventListener('click', async () => {
    await guardar(id, { notas: $('p-notas').value });
    $('p-ok').textContent = 'gespeichert';
    setTimeout(() => ($('p-ok').textContent = ''), 1800);
  });

  const enlaces = [];
  if (d.url_detalle) enlaces.push([d.url_detalle, 'Bekanntmachung auf oeffentlichevergabe.de']);
  (d.urls_pliegos || []).forEach((u, i) => enlaces.push([u, `Vergabeunterlage ${i + 1}`]));
  // Los enlaces se construyen por DOM y la URL se asigna TAL CUAL, como ya se hace en
  // `alternarContratos`. Aquí había un `encodeURI(u)` y rompía todos los enlaces de
  // Las URLs de las plataformas ya vienen percent-encoded de la fuente, y `encodeURI` no
  // respeta el `%`: `%3D` se volvía `%253D`. Con el parámetro doblemente codificado el
  // portal no resuelve el enlace profundo y suelta a quien pulsa en la portada, o el
  // documento no se descarga. Las de DTVP, subreport y los Vergabemarktplätze llevan
  // parámetros codificados igual, así que la regla sigue valiendo: la URL correcta ya
  // está en la base, lo único que hace falta es no tocarla.
  const caja = $('p-enlaces');
  caja.textContent = '';
  if (enlaces.length) {
    for (const [u, t] of enlaces) {
      const a = document.createElement('a');
      a.href = u;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      a.textContent = t;
      caja.appendChild(a);
    }
  } else {
    const vacio = document.createElement('span');
    vacio.className = 'pista';
    vacio.textContent = 'Keine Links veröffentlicht.';
    caja.appendChild(vacio);
  }

  // La fecha del cambio es la que publica la fuente. Cuando no hay ninguna —fichas
  // guardadas antes de que se empezara a registrar— se dice que la fecha es la del día
  // en que la vio el radar, en gris, en vez de hacerla pasar por fecha oficial.
  $('p-hist').innerHTML = (d.historial || []).map((h) =>
    `<li>${h.fecha_cambio
        ? fmtFecha(h.fecha_cambio)
        : `<span style="color:var(--texto-sec)">gesehen am ${fmtFecha(h.detectado_en)}</span>`
      } — ${h.estado_anterior ? `${h.estado_anterior} → ` : ''}${h.estado}` +
    `${h.adjudicatario ? ` · Zuschlag an ${h.adjudicatario}` : ''}</li>`
  ).join('') || '<li>Bisher nur eine Version gesehen.</li>';

  abrirCajon();
}

async function guardar(id, cambios) {
  await fetch('/api/revision', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ licitacion_id: id, ...cambios }),
  });
}

// --- Eventos ---------------------------------------------------------------

let debounce;
$('q').addEventListener('input', () => {
  clearTimeout(debounce);
  debounce = setTimeout(() => cargarLista(), 300);
});
for (const id of ['perfil', 'estado', 'bundesland', 'importe_min', 'orden', 'vivas']) {
  $(id).addEventListener('change', () => {
    // Tocar un filtro a mano deja de corresponder a ningún contador de la cabecera.
    if (id !== 'orden') {
      kpiActivo = null;
      cierranEnDias = '';
      for (const b of $('kpis').querySelectorAll('button.kpi')) b.classList.remove('activo');
    } else {
      try {
        localStorage.setItem(ORDEN_BANDEJA, $('orden').value);
      } catch { /* sin almacén, volverá al de fábrica al recargar */ }
    }
    cargarLista();
  });
}
$('mas').addEventListener('click', () => cargarLista(false));
$('cerrar').addEventListener('click', cerrarCajon);
$('panel-fondo').addEventListener('click', cerrarCajon);
for (const b of document.querySelectorAll('.tab[data-vista]')) {
  b.addEventListener('click', () => mostrarVista(b.dataset.vista));
}
$('analitica-perfil').addEventListener('change', () => {
  perfilAnalitica = $('analitica-perfil').value;
  cargarAnalitica();
});
$('buscar-ahora').addEventListener('click', () => lanzarBusqueda());

// Parar la descarga. El endpoint existía desde el principio y no lo llamaba nadie; hace
// falta desde que la aplicación se pone al día sola al abrirse, porque entonces puede
// empezar a bajar cerca de un giga sin que nadie lo haya pedido en ese momento.
//
// Lo que se para es el proceso de ingesta, no lo ya descargado: la caché se conserva, así
// que retomarlo después no vuelve a bajar lo mismo.
$('carga-parar').addEventListener('click', async () => {
  const btn = $('carga-parar');
  btn.disabled = true;
  btn.textContent = 'Wird abgebrochen …';
  try {
    await fetch('/api/cancelar-busqueda', { method: 'POST' });
  } catch {
    // Si el servidor no contesta, la propia barra desaparecerá sola al ver que ya no hay
    // nada en marcha; no hay nada útil que decir aquí.
  }
  // No hace falta refrescar nada a mano: el vigilante mira cada segundo y medio, verá
  // que ya no hay nada en marcha y esconderá la barra él solo.
  btn.disabled = false;
  btn.textContent = 'Abbrechen';
});
$('previsualizar').addEventListener('click', () => previsualizarAjustes());
$('guardar-perfiles').addEventListener('click', () => guardarAjustes());
$('tab-novedades').addEventListener('click', async () => {
  soloNovedades = !soloNovedades;
  $('tab-novedades').classList.toggle('activo', soloNovedades);
  mostrarVista('bandeja');
  await cargarLista();
  if (!soloNovedades) {
    // Al salir del filtro se marca la visita: lo visto deja de ser novedad.
    await fetch('/api/visita', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    cargarResumen();
  }
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') cerrarCajon();
});

// --- Versión nueva ---------------------------------------------------------

// Solo se abre la boca si hay algo que hacer. Si no hay red, o el repositorio no tiene
// releases, se calla: un cartel de error permanente sobre algo que al usuario no le toca
// arreglar es peor que no decir nada.
async function comprobarVersion() {
  let d;
  try {
    d = await (await fetch('/api/actualizacion')).json();
  } catch {
    return;
  }
  if (!d.hay_nueva) return;

  const caja = $('aviso-version');
  caja.hidden = false;
  caja.innerHTML =
    `<p><strong>Es gibt eine neue Version (${d.version_nueva}).</strong> ` +
    `Installiert ist ${d.version_actual}. ` +
    `<button id="actualizar-ya" class="boton-pri">Jetzt aktualisieren</button> ` +
    `<span id="actualizar-estado" class="pista"></span></p>`;

  $('actualizar-ya').addEventListener('click', async () => {
    const btn = $('actualizar-ya');
    const estado = $('actualizar-estado');
    btn.disabled = true;
    estado.textContent = 'wird geladen und ersetzt …';
    let r;
    try {
      r = await (await fetch('/api/actualizacion', { method: 'POST' })).json();
    } catch (e) {
      estado.textContent = 'konnte nicht abgeschlossen werden; das Programm bleibt unverändert';
      btn.disabled = false;
      return;
    }
    // El mensaje lo redacta Python, que es quien sabe qué ha pasado de verdad y qué se
    // ha tocado. Aquí no se reinterpreta.
    estado.textContent = r.mensaje || '';
    if (r.ok && !r.sin_cambios) btn.remove();
    else btn.disabled = false;
  });
}

try {
  const guardado = localStorage.getItem(ORDEN_BANDEJA);
  if (guardado && [...$('orden').options].some((o) => o.value === guardado)) {
    $('orden').value = guardado;
  }
} catch { /* sin almacén se abre con el de fábrica, que es lo correcto */ }

cargarResumen();
cargarLista();
// Al abrir puede haber ya una carga corriendo por detrás: start.command lanza las
// etapas caras en segundo plano y abre la aplicación acto seguido. Sin esto, el aviso
// no aparecería hasta que alguien pulsara «Buscar ahora».
vigilarBusqueda();
comprobarVersion();

// --- Analítica -------------------------------------------------------------
//
// Barras con divs y no con SVG: se adaptan solas a los dos temas, escalan sin JavaScript
// de redimensionado y llevan la cifra en texto al lado, que es lo que hace que el dato se
// pueda leer y no solo ver.
//
// Regla de pintado, la misma que en `tarjeta()`: el `innerHTML` solo lleva el andamio con
// huecos numéricos o de clase CSS; los nombres de órgano y los objetos de los pliegos van
// por `textContent`, porque vienen de fuentes que no controlamos.

// Sin punto final, aunque el alemán lo lleve al abreviar: son etiquetas de barra y el
// punto cuesta ancho justo en la columna más estrecha. No lo "corrijas".
const MESES_CORTOS = ['Jan', 'Feb', 'Mär', 'Apr', 'Mai', 'Jun',
                      'Jul', 'Aug', 'Sep', 'Okt', 'Nov', 'Dez'];

let rangoAnalitica = 'todo';
let perfilAnalitica = '';

// Los tres rangos. No hay selector libre de fechas a propósito: un comercial quiere «lo de
// este año», y pedir 2019 devolvería una serie de un expediente al mes que se lee como si
// el mercado se hubiera hundido. El corte en 2024 es donde el histórico deja de ser
// residual (2023 son 203 expedientes contra 931 de 2024).
function rangosAnalitica() {
  const hoy = new Date();
  const anio = hoy.getFullYear();
  const hace24 = new Date(hoy.getFullYear(), hoy.getMonth() - 23, 1);
  const mes = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
  return [
    { clave: 'anio', etiqueta: String(anio), desde: `${anio}-01` },
    { clave: '24m', etiqueta: 'letzte 24 Monate', desde: mes(hace24) },
    { clave: 'todo', etiqueta: 'gesamter Zeitraum', desde: '2024-01' },
  ];
}

// `String(42.5)` da «42.5». El resto del fichero resuelve esto con
// `.toFixed(1).replace('.', ',')`; aquí se hace una vez y se reutiliza.
function fmtDecimal(v, decimales = 1) {
  if (v === null || v === undefined) return '—';
  return Number(v).toFixed(decimales).replace('.', ',');
}

function fmtImporteCorto(v) {
  if (v === null || v === undefined) return 'kein Wert';
  // Con separador de millar: el reparto por comunidades llega a «3.402,0 M€», y sin él
  // ese número se lee como 340 o como 34.020 según a quién se le pregunte.
  if (Math.abs(v) >= 1e6) {
    return (v / 1e6).toLocaleString(LOC, { minimumFractionDigits: 1,
                                               maximumFractionDigits: 1 }) + ' Mio. €';
  }
  if (Math.abs(v) >= 1000) return Math.round(v / 1000).toLocaleString(LOC) + ' T€';
  return eur.format(v);
}

function bloqueAnalitica(titulo, pregunta) {
  const el = document.createElement('section');
  el.className = 'bloque';
  el.innerHTML = '<h3></h3><p class="pregunta"></p>';
  el.querySelector('h3').textContent = titulo;
  el.querySelector('.pregunta').textContent = pregunta;
  return el;
}

// `pct` y `encima` son números y se interpolan; `etiqueta` y `texto` son texto y no.
function filaBarra(etiqueta, pct, texto, opciones = {}) {
  const fila = document.createElement('div');
  fila.className = opciones.apilada ? 'fila-barra apilada' : 'fila-barra';
  const clase = opciones.parcial ? 'barra-relleno barra-parcial' : 'barra-relleno';
  const encima = opciones.encima == null ? ''
    : `<div class="barra-encima" style="width:${Number(opciones.encima).toFixed(1)}%"></div>`;
  fila.innerHTML =
    `<span class="etiqueta"></span>` +
    `<div class="barra"><div class="${clase}" style="width:${Number(pct).toFixed(1)}%"></div>` +
    `${encima}</div><span class="valor"></span>`;
  fila.querySelector('.etiqueta').textContent = etiqueta;
  fila.querySelector('.valor').textContent = texto;
  return fila;
}

function nota(el, texto) {
  const p = document.createElement('p');
  p.className = 'nota';
  p.textContent = texto;
  el.appendChild(p);
  return p;
}

function tablaEscueta(el, filas) {
  // `filas` es [[texto, cifra], …]. Las dos celdas por textContent: la primera trae
  // nombres de órgano y objetos de pliegos.
  const tabla = document.createElement('table');
  tabla.className = 'tabla-escueta';
  for (const [texto, cifra, apagado] of filas) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td${apagado ? ' class="apagado"' : ''}></td><td class="num"></td>`;
    tr.children[0].textContent = texto;
    tr.children[1].textContent = cifra;
    tabla.appendChild(tr);
  }
  el.appendChild(tabla);
  return tabla;
}

function insuficiente(el, minimo, que) {
  nota(el, `Noch nicht genug ${que}, um etwas Sinnvolles zu sagen (mindestens ` +
           `${minimo}). Für Historie: python3 radar.py ingest --primera-carga`);
  return el;
}

function pintarCalendario(d) {
  const el = bloqueAnalitica('Wann die Arbeit ausgeschrieben wird',
    'In welchen Monaten muss ich bereit sein – und in welchen passiert nichts?');
  if (!d.suficiente) return insuficiente(el, `${d.minimo_meses} Monate`, 'serie');

  const porMes = {};
  for (const m of d.meses) porMes[m.mes] = m.expedientes;
  const media = {};
  for (const m of d.media_por_mes) media[m.mes] = m.media;

  const hayMedia = d.media_por_mes.length > 0;
  const curso = {};
  if (d.anio_en_curso) {
    for (const m of d.meses) {
      if (m.mes.startsWith(d.anio_en_curso)) curso[m.mes.slice(5, 7)] = m.expedientes;
    }
  }
  const tope = Math.max(1, ...Object.values(media), ...Object.values(curso));

  for (let i = 1; i <= 12; i++) {
    const mm = String(i).padStart(2, '0');
    const enCurso = d.mes_en_curso && d.mes_en_curso.slice(5, 7) === mm;
    const valorMedia = hayMedia ? (media[mm] || 0) : (curso[mm] || 0);
    const valorCurso = hayMedia ? curso[mm] : undefined;
    const texto = valorCurso === undefined
      ? fmtDecimal(valorMedia)
      : `${fmtDecimal(valorMedia)} · ${valorCurso}`;
    el.appendChild(filaBarra(
      MESES_CORTOS[i - 1] + (enCurso ? ' *' : ''),
      100 * valorMedia / tope,
      texto,
      { parcial: enCurso,
        encima: valorCurso === undefined ? null : 100 * valorCurso / tope },
    ));
  }

  if (hayMedia) {
    const leyenda = document.createElement('p');
    leyenda.className = 'leyenda';
    leyenda.innerHTML = '<span class="muestra media"></span> Durchschnitt aus ' +
      '<span id="anios-media"></span> · <span class="muestra curso"></span> ' +
      '<span id="anio-curso"></span>';
    leyenda.querySelector('#anios-media').textContent = d.anios_completos.join(' y ');
    leyenda.querySelector('#anio-curso').textContent = d.anio_en_curso || '';
    el.appendChild(leyenda);
  }
  if (!hayMedia) {
    nota(el, 'Es gibt noch kein vollständiges Kalenderjahr, gezeigt wird also das laufende ' +
             'Jahr: es gibt nichts zum Vergleichen.');
  }
  // El corte sale del último dato descargado, no de hoy: si nadie ingesta en una semana,
  // este número baja solo.
  if (d.mes_en_curso) {
    nota(el, `* ${MESES_CORTOS[Number(d.mes_en_curso.slice(5, 7)) - 1]} läuft noch: ` +
             `Daten bis ${fmtFecha(d.corte)} (${d.dias_con_datos} von ` +
             `${d.dias_del_mes} Tagen), zählt also nicht in den Durchschnitt.`);
  }
  return el;
}

function pintarImportes(d) {
  const el = bloqueAnalitica('Auftragsgrößen',
    'Wie groß sind diese Aufträge, und was übersehe ich, wenn ich nach Wert filtere?');
  el.classList.add('etiquetas-medias');
  const titular = document.createElement('p');
  titular.className = 'titular';
  titular.textContent = fmtImporte(d.mediana);
  const pie = document.createElement('small');
  pie.textContent = ` Median · ${d.con_importe.toLocaleString(LOC)} mit Wert ` +
                    `von ${d.expedientes.toLocaleString(LOC)}`;
  titular.appendChild(pie);
  el.appendChild(titular);

  const tope = Math.max(1, ...d.tramos.map((t) => t.expedientes));
  for (const t of d.tramos) {
    const etiqueta = t.hasta === null
      ? `über ${fmtImporteCorto(t.desde)}`
      : (t.desde === 0 ? `bis ${fmtImporteCorto(t.hasta)}`
                       : `${fmtImporteCorto(t.desde)}–${fmtImporteCorto(t.hasta)}`);
    el.appendChild(filaBarra(etiqueta, 100 * t.expedientes / tope,
                             t.expedientes.toLocaleString(LOC)));
  }
  if (d.mayores.length) {
    nota(el, 'Die fünf größten mit Namen, weil sie die Skala bestimmen und manche ' +
             'zwischen den Quellen doppelt vorkommen:');
    tablaEscueta(el, d.mayores.map((m) => [
      `${m.organo || '(ohne Vergabestelle)'} — ${(m.objeto || '').slice(0, 70)}`,
      fmtImporteCorto(m.imp), true,
    ]));
  }
  nota(el, 'Gezeigt wird der höchste veröffentlichte Wert je Vorgang. Weder Mittelwert noch ' +
           'Summe: einzelne Großaufträge verzerren beides und die Zahl würde ' +
           'weit über dem Median liegen.');
  return el;
}

function pintarBaja(d) {
  const el = bloqueAnalitica('Zu welchem Preis zugeschlagen wird',
    'Wie weit unter dem veröffentlichten Auftragswert wird zugeschlagen?');
  el.classList.add('etiquetas-medias');
  if (!d.suficiente) {
    return insuficiente(el, `${d.minimo_comparables} vergleichbare Zuschläge`, 'datos');
  }
  const titular = document.createElement('p');
  titular.className = 'titular';
  titular.textContent = `${fmtDecimal(d.mediana)} %`;
  const pie = document.createElement('small');
  pie.textContent = ` medianer Preisabschlag · über ${d.comparables.toLocaleString(LOC)} ` +
                    `vergleichbare Zuschläge`;
  titular.appendChild(pie);
  el.appendChild(titular);

  const tope = Math.max(1, ...d.tramos.map((t) => t.expedientes));
  for (const t of d.tramos) {
    const etiqueta = t.hasta === null ? `über ${t.desde} %`
      : (t.desde === 0 ? `bis ${t.hasta} %` : `${t.desde}–${t.hasta}%`);
    el.appendChild(filaBarra(etiqueta, 100 * t.expedientes / tope,
                             t.expedientes.toLocaleString(LOC)));
  }
  // Lo excluido se enseña, no se esconde en un asterisco: es casi la mitad de la muestra.
  nota(el, `Von ${d.con_ambos_importes.toLocaleString(LOC)} Vorgängen mit beiden Werten ` +
           'wurden ausgelassen:');
  tablaEscueta(el, d.excluidos.map((e) => [e.motivo, e.expedientes.toLocaleString(LOC), true]));
  nota(el, 'Verglichen wird gegen den veröffentlichten Auftragswert, nicht gegen den geschätzten ' +
           'Gesamtwert, der Verlängerungen und Änderungen enthält und den Abschlag aufblähen würde.');
  return el;
}

function pintarCiclo(d) {
  const el = bloqueAnalitica('Wann es in den Forecast kommt',
    'Wenn ich es heute veröffentlicht sehe: wann wird entschieden?');
  el.classList.add('etiquetas-medias');
  if (!d.suficiente) {
    return insuficiente(el, `${d.minimo_expedientes} Vorgänge mit Verlauf`, 'historial');
  }
  const titular = document.createElement('p');
  titular.className = 'titular';
  titular.textContent = `${d.mediana_dias} Tage`;
  const pie = document.createElement('small');
  pie.textContent = ` von der Veröffentlichung bis zum Zuschlag · Hälfte zwischen ${d.p25} und ${d.p75}`;
  titular.appendChild(pie);
  el.appendChild(titular);

  const tope = Math.max(1, ...d.tramos.map((t) => t.expedientes));
  for (const t of d.tramos) {
    const etiqueta = t.hasta === null ? `über ${t.desde} T`
      : (t.desde === 0 ? `bis ${t.hasta} T` : `${t.desde}–${t.hasta} d`);
    el.appendChild(filaBarra(etiqueta, 100 * t.expedientes / tope,
                             t.expedientes.toLocaleString(LOC)));
  }
  nota(el, `Gemessen an ${d.expedientes.toLocaleString(LOC)} Vorgängen, die ` +
           'beide Zeitpunkte mit einem von der Quelle veröffentlichten Datum durchlaufen haben.');
  return el;
}

function pintarRenovaciones(d) {
  const el = bloqueAnalitica('Wen ich vor der Ausschreibung anrufe',
    'Wessen Vertrag läuft aus, bevor die neue Ausschreibung herausgeht?');
  const titular = document.createElement('p');
  titular.className = 'titular';
  titular.textContent = d.expedientes.toLocaleString(LOC);
  const pie = document.createElement('small');
  pie.textContent = ` in den nächsten ${d.meses} Monaten · ${d.con_incumbente} mit ` +
                    'Bestandsanbieter bekannt';
  titular.appendChild(pie);
  el.appendChild(titular);
  const enlace = document.createElement('button');
  enlace.className = 'boton-sec';
  enlace.textContent = 'Zur Liste unter Vertragsenden';
  enlace.addEventListener('click', () => mostrarVista('vencimientos'));
  el.appendChild(enlace);
  nota(el, 'Immer zum heutigen Tag: der Zeitraum oben wirkt sich nicht aus.');
  return el;
}

function pintarCpv(d) {
  const el = bloqueAnalitica('Was genau eingekauft wird',
    'Unter welchen CPV-Codes läuft mein Produkt, und lohnt es, dort zu schärfen?');
  const tope = Math.max(1, ...d.divisiones.map((x) => x.expedientes));
  for (const x of d.divisiones) {
    // Apilada: «79 servicios para empresas y seguridad» pide 18,7 em medidos, y en una
    // columna de ese ancho no queda barra que mirar.
    el.appendChild(filaBarra(x.division, 100 * x.expedientes / tope,
                             x.expedientes.toLocaleString(LOC), { apilada: true }));
    // El nombre de la división va aparte: no lo trae la etiqueta.
    el.lastChild.querySelector('.etiqueta').textContent = `${x.division} ${x.nombre}`;
  }
  nota(el, 'Die drei Codes, die buchstäblich dein Produkt sind:');
  tablaEscueta(el, d.del_producto.map((x) => [
    `${x.codigo} · ${x.nombre}`, x.expedientes.toLocaleString(LOC),
  ]));
  const boton = document.createElement('button');
  boton.className = 'boton-sec';
  boton.textContent = 'Suchbegriffe schärfen';
  boton.addEventListener('click', () => mostrarVista('ajustes'));
  el.appendChild(boton);
  nota(el, `Ein Vorgang hat mehrere CPV-Codes, die Zählungen summieren sich also nicht und lassen ` +
           `sich nicht in Prozent aufteilen. ${d.sin_cpv} Vorgänge haben gar keinen.`);
  return el;
}

// Los dos repartos territoriales comparten pintado porque son el mismo gráfico con dos
// preguntas distintas: dónde se ha repartido lo ya cerrado y dónde queda dinero en juego.
function pintarComunidades(d, titulo, pregunta) {
  const el = bloqueAnalitica(titulo, pregunta);
  el.classList.add('etiquetas-anchas');
  if (!d.comunidades.length) {
    nota(el, 'In diesem Zeitraum gibt es keinen Vorgang mit Bundesland und Wert.');
    return el;
  }
  const tope = Math.max(1, ...d.comunidades.map((c) => c.importe));
  for (const c of d.comunidades) {
    el.appendChild(filaBarra(c.bundesland, 100 * c.importe / tope,
                             `${fmtImporteCorto(c.importe)} · ${c.expedientes}`));
  }

  // El total con los grandes dentro va al pie, no en un asterisco: en lo que está vivo
  // son el 96% del dinero, y callarlo dejaría un gráfico que dice que el mercado está
  // repartido cuando son cuatro plataformas de compra.
  if (d.excluidos.length) {
    const n = d.excluidos.length;
    const uno = n === 1;
    const pct = Math.round(100 * d.importe_excluido / d.importe_con_excluidos);
    nota(el, `Las barras suman ${fmtImporteCorto(d.importe_en_barras)}. Contando ` +
             `${uno ? 'der Auftrag' : `die ${n} Aufträge`} über ` +
             `${fmtImporteCorto(d.importe_maximo)}, ${uno ? 'der' : 'die'} ` +
             `außerhalb der Skala liegt, ergibt sich ` +
             `${fmtImporteCorto(d.importe_con_excluidos)}: ${pct} % des Geldes liegen ` +
             `${uno ? 'in diesem Auftrag' : `in diesen ${n}`}.`);
    tablaEscueta(el, d.excluidos.map((e) => [
      `${e.bundesland} · ${(e.objeto || '(ohne Bezeichnung)').slice(0, 55)} — sein Bundesland ` +
      `kommt in Wirklichkeit auf ${fmtImporteCorto(e.total_de_su_comunidad)}`,
      fmtImporteCorto(e.importe), true,
    ]));
    nota(el, `${uno ? 'Er wird' : 'Sie werden'} aus den Balken genommen, weil das keine Aufträge ` +
             'sondern Kontinente — dynamische Beschaffungssysteme und Rahmenvereinbarungen, bei denen man ' +
             'ein Los gewinnt —: in derselben Skala wären die übrigen Länder ein Strich ' +
             'von einem Pixel.');
  } else {
    nota(el, `Verteilt insgesamt: ${fmtImporteCorto(d.importe_con_excluidos)}. Kein ` +
             `Auftrag liegt über ${fmtImporteCorto(d.importe_maximo)}, es wurde also ` +
             'nichts aus der Skala ausgelassen.');
  }

  nota(el, 'Je Vorgang zählt der höchste veröffentlichte Wert, nicht die Summe seiner ' +
           'Lose.' + (d.sin_comunidad
             ? ` ${d.sin_comunidad.toLocaleString(LOC)} Vorgänge bringen kein ` +
               'Bundesland mit – TED veröffentlicht keine Region – und fehlen in der Verteilung.'
             : ''));
  return el;
}

// El hermano por número. Va aparte y no como una opción de `pintarComunidades` porque
// no comparte casi nada: no hay umbral, no hay tabla de apartados y el pie dice otra cosa.
function pintarComunidadesRecuento(d, titulo, pregunta) {
  const el = bloqueAnalitica(titulo, pregunta);
  el.classList.add('etiquetas-anchas');
  if (!d.recuento.length) {
    nota(el, 'In diesem Zeitraum gibt es keinen Vorgang mit Bundesland.');
    return el;
  }
  const tope = Math.max(1, ...d.recuento.map((c) => c.expedientes));
  for (const c of d.recuento) {
    el.appendChild(filaBarra(c.bundesland, 100 * c.expedientes / tope,
                             c.expedientes.toLocaleString(LOC)));
  }
  // Dos tarjetas contiguas con cifras distintas para la misma comunidad parecen un
  // fallo, así que se dice por qué no lo son.
  if (d.excluidos.length) {
    const n = d.excluidos.length;
    const aparta = n === 1
      ? 'einschließlich des Auftrags, den die Wertgrafik aussortiert'
      : `einschließlich der ${n} Aufträge, die die Wertgrafik aussortiert`;
    nota(el, `Gezählt werden die ${d.expedientes_contados.toLocaleString(LOC)} ` +
             `Vorgänge mit Bundesland, ${aparta} aus der Skala: eine Zählung verzerrt ` +
             'ein Großauftrag verzerrt sie, und ihn wegzulassen würde Ausschreibungen verstecken, ' +
             'an denen man teilnehmen kann.');
  }
  nota(el, d.sin_comunidad
    ? `${d.sin_comunidad.toLocaleString(LOC)} Vorgänge bringen kein Bundesland mit – TED ` +
      'veröffentlicht keine Region – und fehlen in der Verteilung.'
    : 'Alle Vorgänge des Zeitraums haben ein Bundesland.');
  return el;
}

function pintarPlazo(d) {
  const el = bloqueAnalitica('Wie viel Zeit für das Angebot bleibt',
    'Wenn ich es heute sehe: reicht die Zeit für ein Angebot?');
  el.classList.add('etiquetas-medias');
  if (!d.suficiente) {
    return insuficiente(el, `${d.minimo_expedientes} Vorgänge mit Frist`, 'datos');
  }
  const titular = document.createElement('p');
  titular.className = 'titular';
  titular.textContent = `${d.mediana_dias} Tage`;
  const pie = document.createElement('small');
  pie.textContent = ` mediane Frist · die Hälfte zwischen ${d.p25} und ${d.p75}`;
  titular.appendChild(pie);
  el.appendChild(titular);

  const tope = Math.max(1, ...d.tramos.map((t) => t.expedientes));
  for (const t of d.tramos) {
    const etiqueta = t.hasta === null ? `über ${t.desde} T`
      : (t.desde === 0 ? `bis ${t.hasta} T` : `${t.desde}–${t.hasta} d`);
    el.appendChild(filaBarra(etiqueta, 100 * t.expedientes / tope,
                             t.expedientes.toLocaleString(LOC)));
  }
  nota(el, `Gemessen an ${d.expedientes.toLocaleString(LOC)} Vorgängen, mit den ` +
           'beiden Daten aus derselben Bekanntmachung. Von ' +
           `${d.con_ambas_fechas.toLocaleString(LOC)} mit beiden Daten wurden ` +
           'dejado fuera:');
  tablaEscueta(el, d.excluidos.map((e) => [
    e.motivo, e.expedientes.toLocaleString(LOC), true,
  ]));
  return el;
}

function pintarProcedimiento(d) {
  const el = bloqueAnalitica('Wie eingekauft wird',
    'Durch welche Tür kommt man rein, und wie oft kann man gar nicht bieten?');
  // Apilada y no en columna: «Verhandlungsverfahren ohne Teilnahmewettbewerb» son unos
  // 22 em contra los 13 de la columna más ancha que hay. Es el mismo caso que el de los
  // nombres de órgano, y por eso reutiliza la variante que ya existía.
  const tope = Math.max(1, ...d.procedimientos.map((x) => x.expedientes));
  for (const x of d.procedimientos) {
    el.appendChild(filaBarra(x.procedimiento, 100 * x.expedientes / tope,
                             x.expedientes.toLocaleString(LOC), { apilada: true }));
  }
  nota(el, 'In einem Verhandlungsverfahren ohne Teilnahmewettbewerb bietet man nicht mit: entweder man ist eingeladen oder nicht, ' +
           'dieser Balken ist also kein Markt zum Mitbieten, sondern Vertriebsarbeit ' +
           'vor der Ausschreibung.');
  if (d.sin_dato) {
    nota(el, `${d.sin_dato.toLocaleString(LOC)} Vorgänge nennen die Verfahrensart ` +
             'nicht. Die Bezeichnungen werden auf die von VgV und UVgO vereinheitlicht.');
  }
  return el;
}

function pintarOrganos(d) {
  const el = bloqueAnalitica('Wer einkauft',
    'Welche Vergabestellen wiederholen sich, und wen lohnt es zu besuchen?');
  const tope = Math.max(1, ...d.organos.map((x) => x.expedientes));
  for (const x of d.organos) {
    el.appendChild(filaBarra(x.organo, 100 * x.expedientes / tope,
                             x.expedientes.toLocaleString(LOC), { apilada: true }));
  }
  nota(el, `${d.distintos.toLocaleString(LOC)} verschiedene Vergabestellen haben etwas veröffentlicht, ` +
           'was zum Radar passt; hier sind die, die sich am häufigsten wiederholen.');
  // Se advierte en lugar de fusionar: una regla de «nombres parecidos» junta cosas que
  // no son la misma, y aquí el coste de equivocarse lo paga quien coja el teléfono.
  nota(el, 'Die Quelle nennt die zeichnende Stelle, nicht die Behörde, dieselbe ' +
           'Einrichtung kann also mehrfach mit verschiedenen Stellen auftauchen.');
  return el;
}

function pintarCartera(d) {
  const el = bloqueAnalitica('Was heute wirklich da ist',
    'Ist das eine Pipeline oder ein historisches Archiv?');
  const fila = document.createElement('div');
  fila.className = 'cifras-fila';
  const cifras = [
    ['Treffer', d.expedientes, ''],
    [`über ${fmtDecimal(d.puntuacion_lista_corta)} Punkte haben`, d.lista_corta, ''],
    ['mit laufender Frist', d.con_plazo_abierto, 'ojo'],
    ['bereits geschlossen oder vergeben', d.expedientes - d.con_plazo_abierto, ''],
  ];
  fila.innerHTML = cifras.map(([, , clase]) =>
    `<div class="${clase}"><b></b><span></span></div>`).join('');
  cifras.forEach(([etiqueta, valor], i) => {
    fila.children[i].querySelector('b').textContent = valor.toLocaleString(LOC);
    fila.children[i].querySelector('span').textContent = etiqueta;
  });
  el.appendChild(fila);

  // La frase sale del dato: el día que esto sea un pipeline de verdad, dejará de decir
  // que es un archivo.
  nota(el, d.es_archivo_historico
    ? `Von ${d.expedientes.toLocaleString(LOC)} Treffern ` +
      `${d.con_plazo_abierto} ` +
      'haben eine laufende Frist: das ist vor allem ein historisches Archiv, sein Wert liegt ' +
      'in den anderen Blöcken, nicht im Abgeben von Angeboten.'
    : `${d.con_plazo_abierto} Treffer haben eine laufende Frist: es gibt eine lebendige Pipeline, an der sich ` +
      'im Posteingang arbeiten lässt.');
  if (d.consultas_previas_sin_importe) {
    nota(el, `${d.consultas_previas_sin_importe} sind Markterkundungen ohne ` +
             'veröffentlichten Wert: sie sind die Tür vor der Ausschreibung, kein Geschäft.');
  }
  tablaEscueta(el, d.estados.map((e) => [e.estado, e.expedientes.toLocaleString(LOC), true]));
  return el;
}

async function cargarAnalitica() {
  const rango = rangosAnalitica().find((r) => r.clave === rangoAnalitica);
  const perfil = perfilAnalitica;
  const carga = empezarCarga('analitica', 'Wird über die Historie berechnet …');
  // Los adornos que viven fuera del contenedor gobernado se limpian a mano: son del
  // rango anterior y dejarlos mientras carga el nuevo es peor que no mostrar nada.
  $('analitica-resumen').textContent = '';

  $('analitica-rango').innerHTML = rangosAnalitica().map((r) =>
    `<button class="ventana ${r.clave === rangoAnalitica ? 'activa' : ''}"
             data-rango="${r.clave}"><div class="t"></div></button>`).join('');
  [...$('analitica-rango').querySelectorAll('.ventana')].forEach((b, i) => {
    b.querySelector('.t').textContent = rangosAnalitica()[i].etiqueta;
    b.addEventListener('click', () => {
      rangoAnalitica = b.dataset.rango;
      cargarAnalitica();
    });
  });

  const cont = $('analitica');
  let d;
  try {
    const p = new URLSearchParams({ desde: rango.desde });
    if (perfil) p.set('perfil', perfil);
    d = await (await fetch('/api/analitica?' + p)).json();
  } catch {
    if (carga.terminar()) {
      cont.innerHTML = '<p class="vacio">Die Auswertung konnte nicht geladen werden.</p>';
    }
    return;
  }
  if (!carga.terminar()) return;
  if (d.error) {
    cont.innerHTML = '<p class="vacio"></p>';
    cont.firstChild.textContent = d.error;
    return;
  }
  if (!d.generado_para.expedientes) {
    cont.innerHTML = `<p class="vacio">Keine Treffer in diesem Zeitraum.
      Für Historie: <code>python3 radar.py ingest --primera-carga</code></p>`;
    return;
  }

  const g = d.generado_para;
  $('analitica-resumen').innerHTML =
    `<strong>${g.expedientes.toLocaleString(LOC)}</strong> Vorgänge ` +
    `(<span id="an-anuncios"></span> Bekanntmachungen) seit <span id="an-desde"></span>` +
    `<span id="an-perfil"></span>.`;
  $('an-anuncios').textContent = g.anuncios.toLocaleString(LOC);
  $('an-desde').textContent = g.desde || 'dem Anfang';
  // Los perfiles no suman: un 9,5% de los expedientes casa con dos o más, así que si
  // alguien suma los filtros le sale más que el total.
  $('an-perfil').textContent = g.perfil
    ? ` · Profil „${g.perfil}“ (manche zählen auch in anderen Profilen)`
    : '';

  // Las filas se escriben aquí y no las decide el CSS porque el emparejado no es
  // estético: cada fila junta dos bloques que se leen del mismo tirón, y el orden es el
  // de la venta —cuándo sale, cuánto vale, a qué precio, cuánto tiempo hay, dónde está,
  // quién compra, cómo— y no el del cálculo. `.pareja` reparte por igual los bloques que
  // le eches, así que la fila de tres no necesita ninguna clase nueva.
  const filas = [
    [pintarCalendario(d.calendario), pintarImportes(d.importes)],
    [pintarBaja(d.baja), pintarPlazo(d.plazo)],
    // Dinero a la izquierda y número a la derecha de la MISMA pregunta: el orden de las
    // dos listas casi nunca coincide —un Land puede ser tercero en euros y segundo en
    // número de operaciones— y ese desajuste es justo lo que hay que ver de un vistazo.
    [pintarComunidades(d.comunidades.adjudicadas,
                       'Top-Bundesländer nach Zuschlägen',
                       'Wo ist das bereits vergebene Geld gelandet?'),
     pintarComunidadesRecuento(d.comunidades.adjudicadas,
                       'Top-Bundesländer nach Anzahl der Zuschläge',
                       'Wo werden die meisten Aufträge vergeben, unabhängig vom Wert?')],
    [pintarComunidades(d.comunidades.activas,
                       'Top-Bundesländer nach laufenden Verfahren',
                       'Wo steht noch Geld im Spiel, solange die Frist läuft?'),
     pintarComunidadesRecuento(d.comunidades.activas,
                       'Top-Bundesländer nach Anzahl laufender Verfahren',
                       'Wo sind gerade die meisten Verfahren offen?')],
    [pintarOrganos(d.organos), pintarCpv(d.cpv)],
    [pintarProcedimiento(d.procedimiento), pintarCiclo(d.ciclo),
     pintarRenovaciones(d.renovaciones)],
  ];
  for (const fila of filas) {
    const caja = document.createElement('div');
    caja.className = 'pareja';
    fila.forEach((b) => caja.appendChild(b));
    cont.appendChild(caja);
  }
  // Va sola y a lo ancho, y la última: es la que dice si todo lo de arriba es un
  // pipeline o un archivo histórico.
  cont.appendChild(pintarCartera(d.cartera));
}
