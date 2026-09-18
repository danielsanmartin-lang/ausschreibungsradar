const fs = require('fs');
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, AlignmentType,
  Table, TableRow, TableCell, WidthType, ShadingType, BorderStyle,
  LevelFormat, Footer, PageNumber, convertInchesToTwip,
} = require('docx');

// --- Constantes de maquetación ---------------------------------------------
const AZUL = '1F4E79';
const AZUL_CLARO = 'DCE6F1';
const GRIS = '595959';
const GRIS_FILA = 'F2F2F2';

// Ancho útil: A4 (11906 dxa) menos márgenes de 1 pulgada a cada lado.
const ANCHO = 11906 - 2 * 1440;

const numbering = {
  config: [
    {
      reference: 'vinetas',
      levels: [
        {
          level: 0, format: LevelFormat.BULLET, text: '•',
          alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: convertInchesToTwip(0.3), hanging: convertInchesToTwip(0.18) } } },
        },
        {
          level: 1, format: LevelFormat.BULLET, text: '◦',
          alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: convertInchesToTwip(0.6), hanging: convertInchesToTwip(0.18) } } },
        },
      ],
    },
    {
      reference: 'pasos',
      levels: [
        {
          level: 0, format: LevelFormat.DECIMAL, text: '%1.',
          alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: convertInchesToTwip(0.3), hanging: convertInchesToTwip(0.18) } } },
        },
      ],
    },
  ],
};

// --- Ayudas ----------------------------------------------------------------
const p = (texto, opciones = {}) =>
  new Paragraph({
    spacing: { after: 140, line: 276 },
    ...opciones,
    children: Array.isArray(texto) ? texto : [new TextRun(texto)],
  });

const vineta = (hijos, nivel = 0) =>
  new Paragraph({
    numbering: { reference: 'vinetas', level: nivel },
    spacing: { after: 80, line: 276 },
    children: Array.isArray(hijos) ? hijos : [new TextRun(hijos)],
  });

const paso = (hijos) =>
  new Paragraph({
    numbering: { reference: 'pasos', level: 0 },
    spacing: { after: 80, line: 276 },
    children: Array.isArray(hijos) ? hijos : [new TextRun(hijos)],
  });

const h1 = (texto, paginaNueva = false) =>
  new Paragraph({
    heading: HeadingLevel.HEADING_1,
    pageBreakBefore: paginaNueva,
    spacing: { before: 320, after: 160 },
    border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: AZUL_CLARO, space: 6 } },
    children: [new TextRun({ text: texto, bold: true, size: 30, color: AZUL })],
  });

const h2 = (texto) =>
  new Paragraph({
    heading: HeadingLevel.HEADING_2,
    spacing: { before: 240, after: 120 },
    children: [new TextRun({ text: texto, bold: true, size: 24, color: AZUL })],
  });

const h3 = (texto) =>
  new Paragraph({
    heading: HeadingLevel.HEADING_3,
    spacing: { before: 180, after: 100 },
    children: [new TextRun({ text: texto, bold: true, size: 22, color: GRIS })],
  });

const codigo = (lineas) =>
  lineas.map((l, i) =>
    new Paragraph({
      spacing: { after: i === lineas.length - 1 ? 160 : 0, line: 240 },
      shading: { type: ShadingType.CLEAR, fill: 'F5F5F5' },
      indent: { left: convertInchesToTwip(0.15) },
      children: [new TextRun({ text: l || ' ', font: 'Consolas', size: 17 })],
    })
  );

// Tabla con anchos duales (tabla y celdas) en DXA, como exige Word y Google Docs.
function tabla(cabeceras, filas, proporciones) {
  const total = proporciones.reduce((a, b) => a + b, 0);
  const anchos = proporciones.map((x) => Math.round((x / total) * ANCHO));
  // Corrige el redondeo para que la suma cuadre exactamente con el ancho.
  anchos[anchos.length - 1] += ANCHO - anchos.reduce((a, b) => a + b, 0);

  const celda = (texto, i, opciones = {}) =>
    new TableCell({
      width: { size: anchos[i], type: WidthType.DXA },
      margins: { top: 70, bottom: 70, left: 110, right: 110 },
      shading: opciones.fill ? { type: ShadingType.CLEAR, fill: opciones.fill } : undefined,
      children: [
        new Paragraph({
          spacing: { after: 0, line: 252 },
          children: [
            new TextRun({
              text: String(texto),
              bold: !!opciones.bold,
              size: 18,
              color: opciones.bold ? AZUL : undefined,
              font: opciones.mono ? 'Consolas' : undefined,
            }),
          ],
        }),
      ],
    });

  return new Table({
    width: { size: ANCHO, type: WidthType.DXA },
    columnWidths: anchos,
    borders: {
      top: { style: BorderStyle.SINGLE, size: 4, color: 'BFBFBF' },
      bottom: { style: BorderStyle.SINGLE, size: 4, color: 'BFBFBF' },
      left: { style: BorderStyle.SINGLE, size: 4, color: 'BFBFBF' },
      right: { style: BorderStyle.SINGLE, size: 4, color: 'BFBFBF' },
      insideHorizontal: { style: BorderStyle.SINGLE, size: 2, color: 'D9D9D9' },
      insideVertical: { style: BorderStyle.SINGLE, size: 2, color: 'D9D9D9' },
    },
    rows: [
      new TableRow({
        tableHeader: true,
        children: cabeceras.map((c, i) => celda(c, i, { bold: true, fill: AZUL_CLARO })),
      }),
      ...filas.map((fila, n) =>
        new TableRow({
          children: fila.map((c, i) =>
            celda(c, i, { fill: n % 2 ? GRIS_FILA : undefined, mono: !!(proporciones.mono || []).includes(i) })
          ),
        })
      ),
    ],
  });
}

const espacio = (alto = 120) => new Paragraph({ spacing: { after: alto }, children: [new TextRun('')] });

// Diagrama de capas. Se dibuja con una tabla porque es lo que Word, Pages y Google
// Docs renderizan igual; una imagen o una autoforma se degradan de formas distintas
// en cada uno.
function diagrama() {
  const CAPAS = [
    { etiqueta: 'FUENTES', fill: 'E8EEF7',
      cajas: ['PLACSP\nlicitaciones', 'PLACSP\nagregadas', 'PLACSP\nconsultas previas', 'TED\nUnión Europea', 'Cataluña\nSocrata'] },
    { etiqueta: 'CONECTORES', fill: 'DCE6F1',
      cajas: ['placsp.py — ATOM / CODICE 2.07', 'ted.py — Search API v3', 'catalunya.py — SoQL'] },
    { etiqueta: 'NÚCLEO', fill: 'D0DEEF',
      cajas: ['net.py\nTLS + reintentos', 'model.py\nmodelo común', 'db.py\ndedup + historial', 'matching.py\nmotor de reglas'] },
    { etiqueta: 'ALMACÉN', fill: 'C4D6EA',
      cajas: ['data/ausschreibungen.db — SQLite con índice de texto completo (FTS5)'] },
    { etiqueta: 'CONSULTA', fill: 'DCE6F1',
      cajas: ['consultas.py — contadores, bandeja, vencimientos, competencia'] },
    { etiqueta: 'INTERFAZ', fill: 'E8EEF7',
      cajas: ['Bandeja', 'Vencimientos', 'Adjudicatarios', 'Términos de búsqueda', 'CLI radar.py'] },
  ];

  const ANCHO_ETIQUETA = 1500;
  const ANCHO_CAJAS = ANCHO - ANCHO_ETIQUETA;

  const filas = [];
  CAPAS.forEach((capa, indice) => {
    const n = capa.cajas.length;
    const anchoCaja = Math.floor(ANCHO_CAJAS / n);
    const anchos = Array(n).fill(anchoCaja);
    anchos[n - 1] += ANCHO_CAJAS - anchoCaja * n;

    filas.push(
      new TableRow({
        children: [
          new TableCell({
            width: { size: ANCHO_ETIQUETA, type: WidthType.DXA },
            margins: { top: 80, bottom: 80, left: 100, right: 100 },
            verticalAlign: 'center',
            children: [
              new Paragraph({
                spacing: { after: 0 },
                children: [new TextRun({ text: capa.etiqueta, bold: true, size: 15, color: AZUL })],
              }),
            ],
          }),
          new TableCell({
            width: { size: ANCHO_CAJAS, type: WidthType.DXA },
            margins: { top: 60, bottom: 60, left: 60, right: 60 },
            children: [
              new Table({
                width: { size: ANCHO_CAJAS - 120, type: WidthType.DXA },
                columnWidths: anchos.map((a) => a - Math.floor(120 / n)),
                borders: {
                  top: { style: BorderStyle.NONE }, bottom: { style: BorderStyle.NONE },
                  left: { style: BorderStyle.NONE }, right: { style: BorderStyle.NONE },
                  insideHorizontal: { style: BorderStyle.NONE },
                  insideVertical: { style: BorderStyle.SINGLE, size: 8, color: 'FFFFFF' },
                },
                rows: [
                  new TableRow({
                    children: capa.cajas.map((texto, i) =>
                      new TableCell({
                        width: { size: anchos[i] - Math.floor(120 / n), type: WidthType.DXA },
                        shading: { type: ShadingType.CLEAR, fill: capa.fill },
                        margins: { top: 90, bottom: 90, left: 80, right: 80 },
                        verticalAlign: 'center',
                        children: texto.split('\n').map((linea, j) =>
                          new Paragraph({
                            alignment: AlignmentType.CENTER,
                            spacing: { after: 0, line: 230 },
                            children: [new TextRun({ text: linea, size: 15, bold: j === 0 })],
                          })
                        ),
                      })
                    ),
                  }),
                ],
              }),
            ],
          }),
        ],
      })
    );

    // Flecha de descenso entre capas.
    if (indice < CAPAS.length - 1) {
      filas.push(
        new TableRow({
          children: [
            new TableCell({
              width: { size: ANCHO_ETIQUETA, type: WidthType.DXA },
              children: [new Paragraph({ spacing: { after: 0 }, children: [new TextRun('')] })],
            }),
            new TableCell({
              width: { size: ANCHO_CAJAS, type: WidthType.DXA },
              margins: { top: 0, bottom: 0 },
              children: [
                new Paragraph({
                  alignment: AlignmentType.CENTER,
                  spacing: { before: 20, after: 20, line: 200 },
                  children: [new TextRun({ text: '▼', size: 13, color: '8FAADC' })],
                }),
              ],
            }),
          ],
        })
      );
    }
  });

  return new Table({
    width: { size: ANCHO, type: WidthType.DXA },
    columnWidths: [ANCHO_ETIQUETA, ANCHO_CAJAS],
    borders: {
      top: { style: BorderStyle.NONE }, bottom: { style: BorderStyle.NONE },
      left: { style: BorderStyle.NONE }, right: { style: BorderStyle.NONE },
      insideHorizontal: { style: BorderStyle.NONE }, insideVertical: { style: BorderStyle.NONE },
    },
    rows: filas,
  });
}

// --- Contenido -------------------------------------------------------------
const hijos = [];

// Portada
hijos.push(
  new Paragraph({ spacing: { before: 2600, after: 0 }, alignment: AlignmentType.CENTER,
    children: [new TextRun({ text: 'Ausschreibungsradar', bold: true, size: 60, color: AZUL })] }),
  new Paragraph({ spacing: { before: 160, after: 0 }, alignment: AlignmentType.CENTER,
    children: [new TextRun({ text: 'Detección de licitaciones públicas de concienciación en ciberseguridad y protección del correo electrónico', size: 24, color: GRIS })] }),
  new Paragraph({ spacing: { before: 700, after: 0 }, alignment: AlignmentType.CENTER,
    children: [new TextRun({ text: 'Descripción funcional y arquitectura', bold: true, size: 26 })] }),
  new Paragraph({ spacing: { before: 1400, after: 0 }, alignment: AlignmentType.CENTER,
    children: [new TextRun({ text: 'Agosto de 2026', size: 20, color: GRIS })] }),
  new Paragraph({ spacing: { before: 60 }, alignment: AlignmentType.CENTER,
    children: [new TextRun({ text: 'Herramienta interna · Zepo', size: 20, color: GRIS })] }),
);

// Índice estático: se ve siempre, sin depender de que el lector actualice campos.
const INDICE = [
  ['1.', 'Resumen ejecutivo'],
  ['2.', 'Qué hace la aplicación'],
  ['', 'Bandeja · Vencimientos · Adjudicatarios · Términos de búsqueda · Actualización'],
  ['3.', 'Fuentes de datos'],
  ['4.', 'Cómo decide qué interesa'],
  ['', 'Modelo de tres niveles · Lecciones de los datos reales'],
  ['5.', 'Arquitectura'],
  ['', 'Principios · Flujo de datos · Módulos'],
  ['6.', 'Modelo de datos'],
  ['7.', 'Decisiones técnicas destacables'],
  ['8.', 'Limitaciones conocidas'],
  ['9.', 'Instalación y uso'],
];
hijos.push(h1('Índice', true));
INDICE.forEach(([num, titulo]) => {
  hijos.push(
    new Paragraph({
      spacing: { after: num ? 60 : 120, line: 264 },
      indent: { left: convertInchesToTwip(num ? 0 : 0.32) },
      children: [
        num
          ? new TextRun({ text: num + '  ', bold: true, color: AZUL })
          : new TextRun({ text: '' }),
        new TextRun({ text: titulo, bold: !!num, size: num ? 22 : 19, color: num ? undefined : GRIS }),
      ],
    })
  );
});

// 1. Resumen
hijos.push(
  h1('1. Resumen ejecutivo', true),
  p([
    new TextRun('Ausschreibungsradar es una aplicación de escritorio que rastrea a diario las plataformas de contratación pública españolas y europeas, y presenta en una única bandeja las licitaciones relacionadas con '),
    new TextRun({ text: 'concienciación en ciberseguridad, formación, phishing simulado y protección del correo electrónico', bold: true }),
    new TextRun('. Su objetivo es que un comercial detecte una oportunidad el día que se publica, sin revisar manualmente decenas de portales.'),
  ]),
  p('El problema que resuelve no es acceder a los datos, que son públicos, sino separar la señal del ruido. Las administraciones publican decenas de miles de anuncios al mes y las licitaciones de este nicho aparecen mal clasificadas, redactadas en catalán, escondidas como un lote de un contrato genérico o directamente sin código CPV. La aplicación aplica un motor de reglas de tres niveles que deja pasar en torno al 0,5 % de lo que descarga, y explica de cada resultado por qué ha entrado.'),
  h2('Cifras del sistema en producción'),
  tabla(
    ['Indicador', 'Valor'],
    [
      ['Licitaciones descargadas y normalizadas', '127.552'],
      ['Coincidencias con los perfiles de búsqueda', '640 expedientes'],
      ['Abiertas con plazo de presentación vigente', '53'],
      ['Contratos con adjudicatario identificado', '99.039'],
      ['Contratos que vencen en los próximos 6 meses', '28, por 38,7 M€'],
      ['Fuentes de datos activas', '5'],
      ['Cobertura temporal del histórico', '2021 – 2026'],
      ['Pruebas automatizadas', '126'],
      ['Dependencias externas', 'Ninguna'],
    ],
    [62, 38],
  ),
  espacio(),
  p([
    new TextRun({ text: 'Autonomía. ', bold: true }),
    new TextRun('La aplicación no se conecta a ningún CRM, ni a notas personales, ni a servicios de terceros. Todo reside en su propia carpeta, de modo que cualquier miembro del equipo puede copiarla y usarla sin configuración ni credenciales.'),
  ]),
);

// 2. Qué hace
hijos.push(
    h1('2. Qué hace la aplicación', true),
  p('La interfaz se organiza en cuatro pestañas, cada una orientada a un momento distinto del trabajo comercial.'),

  h2('2.1 Bandeja: qué hay abierto ahora'),
  p('Lista las licitaciones que han superado el filtro, ordenadas por defecto según los días que quedan para presentar oferta, que es el dato que condiciona la decisión. De cada una se muestra el órgano de contratación, el importe, el plazo, la comunidad autónoma, el procedimiento y los enlaces a los pliegos oficiales.'),
  vineta([new TextRun({ text: 'Por qué ha entrado. ', bold: true }), new TextRun('Al abrir una licitación, la ficha indica exactamente qué término o qué código CPV la ha hecho aparecer. Es la información que permite afinar la búsqueda con criterio en lugar de a ciegas.')]),
  vineta([new TextRun({ text: 'Triaje. ', bold: true }), new TextRun('Cada licitación se marca como Seguir, Presentada o Descartar, con notas libres. Al descartar se pide el motivo (fuera de nicho, importe bajo, incumbente atado, fuera de plazo), y esos motivos se agregan para ajustar los perfiles con datos.')]),
  vineta([new TextRun({ text: 'Agrupación del ciclo de vida. ', bold: true }), new TextRun('Un mismo expediente genera varios anuncios a lo largo de su vida: licitación, correcciones y adjudicación por lotes. La bandeja los agrupa en una sola fila con una etiqueta «N anuncios» y muestra el estado más avanzado.')]),
  vineta([new TextRun({ text: 'Novedades. ', bold: true }), new TextRun('Cuando entran licitaciones nuevas desde la última visita aparece un contador para ver solo esas.')]),
  vineta([new TextRun({ text: 'Búsqueda y filtros. ', bold: true }), new TextRun('Texto libre sobre objeto, órgano, expediente y descripción de lotes, más filtros por perfil, comunidad autónoma, importe mínimo y estado de triaje. Exportación a CSV de lo que se esté viendo.')]),

  h2('2.2 Vencimientos: llegar antes de la renovación'),
  p('Lista los contratos ya adjudicados cuyo plazo de ejecución termina pronto, junto con el importe y la empresa que lo ganó. Es la vista de prospección proactiva: permite contactar con el organismo antes de que salga el nuevo pliego, cuando todavía se puede influir en su redacción.'),
  p('Los botones de ventana temporal muestran cuántos contratos vencen en cada plazo y por qué importe agregado, para poder dimensionar la oportunidad de un vistazo:'),
  tabla(
    ['Ventana', 'Contratos', 'Importe agregado'],
    [
      ['3 meses', '13', '15,6 M€'],
      ['6 meses', '28', '38,7 M€'],
      ['12 meses', '37', '44,1 M€'],
      ['24 meses', '69', '72,1 M€'],
    ],
    [34, 33, 33],
  ),
  espacio(),
  p('La fecha de fin se obtiene de la fecha explícita publicada por la fuente o, en su defecto, se calcula sumando la duración del contrato a su fecha de inicio o adjudicación. Si la fuente no publica ninguno de esos datos, la licitación no aparece en esta vista: se prefiere no mostrarla antes que mostrar una fecha estimada sobre la que se llamaría a un cliente.'),

  h2('2.3 Adjudicatarios: contra quién se compite'),
  p('Ranking de las empresas que se están llevando estos contratos, con el número de contratos, el importe acumulado y en cuántos organismos distintos operan. Al pulsar una empresa se despliega el detalle de sus contratos con enlace a cada expediente.'),
  p('Las variantes de razón social se agrupan automáticamente, porque las administraciones escriben el mismo proveedor de formas distintas: «S2 GRUPO SOLUCIONES DE SEGURIDAD, S.L.U.» y «S2 Grupo Soluciones de Seguridad S.L.» son la misma empresa y sin normalizar aparecían como dos. Los tres primeros del ranking actual son Telefónica Soluciones (36 contratos), S2 Grupo (16) y Babel Sistemas de Información (13).'),

  h2('2.4 Términos de búsqueda: ajustar el filtro'),
  p('Pantalla de configuración de los perfiles de búsqueda, sin necesidad de editar ficheros. Cada perfil tiene una caja por tipo de término, importe mínimo y activación. Antes de guardar, la función de previsualización recorre todo lo descargado y responde cuántas licitaciones entrarían y saldrían con los términos nuevos, con ejemplos concretos de ambas.'),
  p('Esa previsualización no es un adorno: modificar un término a ciegas sobre más de cien mil registros es la vía más rápida para llenar la bandeja de ruido y dejar de usar la herramienta. Al guardar se conserva una copia del fichero anterior y se reevalúa todo lo descargado sin volver a descargar nada.'),

  h2('2.5 Actualización de los datos'),
  vineta([new TextRun({ text: 'Botón «Buscar ahora». ', bold: true }), new TextRun('Lanza la descarga en segundo plano mostrando el progreso por fuente, sin bloquear la interfaz. Junto al botón se indica la fecha y hora de la última búsqueda correcta.')]),
  vineta([new TextRun({ text: 'Tarea programada. ', bold: true }), new TextRun('Opcionalmente se instala una tarea de macOS que descarga las novedades cada mañana. Un cerrojo impide que la tarea automática y el botón se solapen, ya que dos procesos escribiendo a la vez competirían por el bloqueo de la base de datos.')]),
  vineta([new TextRun({ text: 'Salud de las fuentes. ', bold: true }), new TextRun('La aplicación avisa cuando una fuente falla o deja de devolver registros. Sin ese aviso, «esta semana no hay licitaciones» y «el conector está roto» resultan indistinguibles.')]),
);

// 3. Fuentes
hijos.push(
    h1('3. Fuentes de datos', true),
  p('El sistema integra cinco fuentes oficiales. Cada conector es independiente: si una plataforma cambia su formato o deja de responder, el resto de la ingesta continúa y la aplicación lo refleja.'),
  tabla(
    ['Fuente', 'Acceso técnico', 'Aporta'],
    [
      ['PLACSP — licitaciones', 'ATOM / CODICE 2.07', 'Estado, comunidades autónomas y la mayoría de entidades locales'],
      ['PLACSP — plataformas agregadas', 'ATOM / CODICE 2.07', 'País Vasco, Navarra y Galicia, además de Cataluña, Andalucía y Madrid'],
      ['PLACSP — consultas preliminares', 'ATOM / CODICE 2.07', 'La fase previa al pliego, donde aún se puede influir'],
      ['TED (Unión Europea)', 'Search API v3 (REST)', 'Contratos sobre umbral europeo y anuncios de adjudicación'],
      ['Cataluña', 'API Socrata (SoQL)', 'Su plataforma propia, con duración de contrato, lotes y adjudicatario'],
    ],
    [26, 22, 52],
  ),
  espacio(),
  p([
    new TextRun({ text: 'Consultas preliminares de mercado. ', bold: true }),
    new TextRun('Merecen mención propia por su valor comercial: son la administración preguntando al mercado antes de redactar el pliego. En esa fase se puede influir en los requisitos; cuando el anuncio se publica, ya solo queda competir.'),
  ]),
  h2('3.1 Filtrado en origen frente a filtrado local'),
  p('TED y Cataluña permiten filtrar en su servidor, así que se les envía únicamente un subconjunto de términos discriminantes. La elección se hizo midiendo: enviar también los términos ambiguos hacía que TED devolviera casi 6.000 avisos, y añadir las palabras de contexto lo elevaba a más de 12.000, para quedarse en los mismos resultados útiles. Con términos discriminantes la consulta devuelve unos cientos. PLACSP no ofrece filtrado, de modo que se descarga completa y se filtra en local.'),
);

// 4. Motor de filtrado
hijos.push(
    h1('4. Cómo decide qué interesa', true),
  p('Es el componente que determina si la herramienta resulta útil o se abandona. El diseño responde a dos hallazgos medidos sobre datos reales.'),
  p([
    new TextRun({ text: 'El código CPV no basta. ', bold: true }),
    new TextRun('De cinco licitaciones de concienciación verificadas en Cataluña, aparecían clasificadas con los CPV 80511000, 80510000, 79341000, 71316000 y una sin CPV alguno. Filtrar por CPV habría perdido cuatro de las cinco.'),
  ]),
  p([
    new TextRun({ text: 'El texto libre por sí solo tampoco. ', bold: true }),
    new TextRun('Buscar «concienciación» trae campañas de feminización de la pobreza, seguridad vial, consumo responsable y prevención de riesgos laborales.'),
  ]),
  h2('4.1 Modelo de tres niveles'),
  tabla(
    ['Elemento', 'Función'],
    [
      ['Términos fuertes', 'Se bastan solos: phishing, DMARC, ingeniería social, oficina de concienciación'],
      ['Términos ambiguos', 'Solo entran si además aparece contexto: concienci, formación, sensibilización'],
      ['Contexto requerido', 'Confirma que trata de seguridad: ciberseguridad, malware, ISO 27001, ENS'],
      ['Prefijos CPV', 'Suman puntuación, pero nunca aceptan por sí solos ni valen como contexto'],
      ['Exclusiones', 'Prevalecen sobre todo lo demás: seguridad vial, riesgos laborales, seguridad privada'],
      ['Importe mínimo', 'Solo descarta cuando el importe se conoce; nunca por dato ausente'],
    ],
    [26, 74],
  ),
  espacio(),
  p('Cada coincidencia recibe una puntuación y, sobre todo, un motivo textual que se conserva en la base de datos. Sin esa traza no se puede afinar el ruido, y el ruido es lo que hace que una herramienta así deje de abrirse a las dos semanas.'),
  h2('4.2 Lecciones de los datos reales'),
  p('Cuatro reglas aprendidas durante la construcción, incorporadas a la configuración y protegidas con pruebas automatizadas:'),
  paso([new TextRun({ text: 'Usar raíces, no palabras completas. ', bold: true }), new TextRun('Los pliegos emplean tanto el verbo como el sustantivo. La raíz «conscienci» cubre conscienciar, conscienciació y concienciación.')]),
  paso([new TextRun({ text: 'Incluir las erratas frecuentes. ', bold: true }), new TextRun('«Phising», con una sola s, aparece literalmente en pliegos publicados. Sin esa variante se perdía un contrato real de 50.000 €.')]),
  paso([new TextRun({ text: 'Contemplar el catalán. ', bold: true }), new TextRun('Buena parte de los textos están en catalán: hay que incluir ciberseguridad y ciberseguretat. Los acentos son indiferentes porque el texto se normaliza.')]),
  paso([new TextRun({ text: 'Vigilar las siglas cortas. ', bold: true }), new TextRun('«SPF» parecía inofensivo y colaba diez licitaciones absurdas: crema solar de factor 50 en socorrismo de playas, ratones SPF de laboratorio y «Entidades SPF» (Sector Público Foral). Se degradó a término ambiguo, que exige contexto de seguridad.')]),
  h2('4.3 Un caso que ilustra el diseño'),
  p('Un contrato de mantenimiento de hardware del Ayuntamiento de Viladecans incluía como lote 10 un «programa de conscienciació en CIBERSEGURETAT». Ni el título ni el CPV del contrato lo delatan. Aparece porque el sistema indexa también el texto de todos los lotes, que es precisamente lo que un filtro por CPV o una búsqueda por título habrían perdido.'),
);

// 5. Arquitectura
hijos.push(
    h1('5. Arquitectura', true),
  h2('5.1 Principios de diseño'),
  vineta([new TextRun({ text: 'Cero dependencias externas. ', bold: true }), new TextRun('Todo el sistema usa exclusivamente la biblioteca estándar de Python 3 y un frontend estático sin compilación. No hay que instalar librerías, ni un gestor de paquetes, ni una base de datos. Una prueba automatizada verifica esta condición en cada ejecución.')]),
  vineta([new TextRun({ text: 'Conectores aislados. ', bold: true }), new TextRun('Cada fuente falla por separado y se registra por separado.')]),
  vineta([new TextRun({ text: 'Sin credenciales ni servicios. ', bold: true }), new TextRun('Ninguna fuente requiere autenticación. La base de datos es un fichero local.')]),
  vineta([new TextRun({ text: 'Un solo origen de verdad por decisión. ', bold: true }), new TextRun('Los contadores de la cabecera y la lista comparten la misma función de filtrado, de modo que no pueden mostrar cifras distintas.')]),
  vineta([new TextRun({ text: 'Nunca inventar un dato. ', bold: true }), new TextRun('Si una fuente no publica una fecha o un importe, el campo queda vacío en lugar de estimarse.')]),

  h2('5.2 Esquema general'),
  p('Las cinco fuentes entran por conectores independientes, se traducen a un modelo común, se depositan en una única base local y de ahí las consumen las cuatro vistas de la aplicación.'),
  diagrama(),
  espacio(160),

  h2('5.3 Flujo de datos'),
  tabla(
    ['Etapa', 'Qué ocurre'],
    [
      ['1. Descarga', 'Cada conector pide a su fuente lo publicado desde la última ejecución, con TLS verificado, reintentos y control de cursor'],
      ['2. Normalización', 'Los tres formatos nativos (ATOM/CODICE, JSON de TED, Socrata) se traducen a un único modelo común'],
      ['3. Deduplicación', 'Se compara una huella del contenido relevante: solo se guarda si algo ha cambiado de verdad'],
      ['4. Historial', 'Los cambios con significado comercial (estado, adjudicatario, importe) generan una versión'],
      ['5. Evaluación', 'El motor de reglas aplica los perfiles y registra la puntuación y el motivo de cada coincidencia'],
      ['6. Presentación', 'Un servidor local sirve la interfaz y una API JSON de solo lectura sobre 127.0.0.1'],
    ],
    [22, 78],
  ),
  espacio(),

  h2('5.4 Módulos'),
  tabla(
    ['Módulo', 'Responsabilidad', 'Líneas'],
    [
      ['radar.py', 'Interfaz de línea de comandos: ingest, match, serve, vencimientos, adjudicatarios, export, programar, estado', '327'],
      ['radar/net.py', 'Descargas con TLS verificado, reintentos con espera creciente y caché condicional', '217'],
      ['radar/model.py', 'Modelo común, normalización de fechas e importes, cálculo de huella y clave de agrupación', '320'],
      ['radar/db.py', 'Esquema SQLite, migraciones idempotentes, deduplicación e historial de versiones', '534'],
      ['radar/matching.py', 'Motor de reglas, validación y previsualización de perfiles', '439'],
      ['radar/consultas.py', 'Consultas de las cuatro vistas, contadores y exportación', '587'],
      ['radar/server.py', 'Servidor HTTP local y API JSON', '306'],
      ['radar/pipeline.py', 'Orquestación de la ingesta y aislamiento de fallos por fuente', '125'],
      ['radar/busqueda.py', 'Lanzamiento en segundo plano y cerrojo entre procesos', '160'],
      ['radar/programar.py', 'Instalación y retirada de la tarea diaria de macOS', '89'],
      ['radar/sources/placsp.py', 'Conector de PLACSP: ATOM CODICE, cuatro conjuntos de datos, histórico en ZIP', '504'],
      ['radar/sources/ted.py', 'Conector de TED: Search API v3, campos multilingües y paginación', '317'],
      ['radar/sources/catalunya.py', 'Conector de Cataluña: API Socrata con filtrado SoQL', '269'],
      ['web/', 'Interfaz: HTML, CSS y JavaScript sin compilación ni librerías', '822'],
    ],
    [24, 62, 14],
  ),
  espacio(),
  p('En total unas 5.850 líneas de Python y 822 de JavaScript, con 126 pruebas automatizadas que emplean descargas reales de las fuentes como material de prueba.'),
);

// 6. Modelo de datos
hijos.push(
    h1('6. Modelo de datos', true),
  p('Una única base SQLite en la carpeta del proyecto. Ocho tablas, de las cuales dos concentran las decisiones de diseño.'),
  tabla(
    ['Tabla', 'Contenido', 'Registros'],
    [
      ['licitaciones', 'Estado actual de cada anuncio, con clave (fuente, identificador externo)', '127.552'],
      ['licitaciones_versiones', 'Un registro por cada cambio con significado comercial', '269.518'],
      ['licitaciones_fts', 'Índice de texto completo, insensible a acentos', '127.552'],
      ['matches', 'Coincidencias con cada perfil, con puntuación y motivo', '921'],
      ['revisiones', 'Triaje humano: estado, notas y motivo de descarte', 'según el uso'],
      ['fuentes_cursor', 'Por dónde se quedó cada fuente en la última ingesta', '5'],
      ['ingest_log', 'Histórico de ejecuciones para vigilar la salud de las fuentes', 'acumulativo'],
      ['preferencias', 'Ajustes locales de quien usa la herramienta', 'pocas'],
    ],
    [24, 58, 18],
  ),
  espacio(),
  h2('6.1 Por qué existe el historial de versiones'),
  p('La especificación de sindicación de PLACSP establece que una licitación se republica tantas veces como se modifique. Sin deduplicación por contenido, la bandeja mostraría el mismo expediente una y otra vez y la herramienta sería inservible en una semana. El historial, además, aporta dos capacidades sin coste añadido: detectar la transición de publicada a adjudicada, con el nombre del ganador y el importe, y conocer la fecha de vencimiento para llegar a tiempo a la renovación.'),
  p('Solo se registra versión cuando cambia el estado, el adjudicatario o el importe. Registrar cualquier cambio de metadatos habría añadido decenas de miles de entradas sin valor y habría convertido el historial en ruido.'),
  h2('6.2 Agrupación por expediente'),
  p('Un expediente genera varios anuncios y cada uno es un registro distinto. En PLACSP el número de expediente se mantiene entre anuncios y sirve de clave; en TED el identificador es propio de cada anuncio, así que la agrupación se hace por órgano y título, descartando el prefijo genérico con que TED encabeza sus títulos. Ese detalle importa: usar el título completo agrupaba contratos sin relación que solo compartían el encabezado, lo que ocultaba licitaciones de la bandeja, un efecto peor que el duplicado que se pretendía evitar.'),
);

// 7. Decisiones técnicas
hijos.push(
    h1('7. Decisiones técnicas destacables', true),
  h2('7.1 Certificados de la administración española'),
  p('Varias plataformas públicas, entre ellas PLACSP, presentan cadenas de certificados firmadas por raíces ausentes del almacén por defecto de macOS: AC RAIZ FNMT-RCM SERVIDORES SEGUROS e Izenpe. Sin ellas la conexión falla con un error de certificado autofirmado que no sugiere en absoluto la causa real.'),
  p('Se detectó además que los intérpretes de Python instalados desde python.org suelen venir sin ninguna raíz cargada, de modo que depender del almacén del sistema haría que la herramienta funcionase en un equipo y fallase en el de al lado. La solución es un paquete de certificados propio, verificado al arrancar, y en ningún caso se desactiva la validación TLS.'),

  h2('7.2 Rendimiento del motor de reglas'),
  p('Evaluar los perfiles sobre 127.552 registros tardaba 8,7 segundos porque el texto se normalizaba una vez por perfil. Se resolvió guardando el texto ya normalizado en la propia base y añadiendo un cribado previo con una única expresión regular por perfil, que descarta de una pasada los registros sin ningún término candidato. El resultado es idéntico y el tiempo baja a poco más de dos segundos, lo que hace viable la previsualización interactiva.'),

  h2('7.3 Tamaño de la base de datos'),
  p('Una revisión del almacenamiento reveló que el 46 % de la base —534 MB— eran copias completas en JSON de cada versión que la aplicación no leía en ningún punto. Se redujeron a los campos que realmente se consultan, con lo que la base pasó de 1.157 MB a 486 MB conservando el historial íntegro.'),

  h2('7.4 Paginación estable'),
  p('Las ordenaciones no incluían ninguna columna única al final, de modo que con desempates numerosos SQLite no garantizaba el mismo orden entre dos consultas: la segunda página podía repetir filas de la primera y omitir otras. Se añadió el identificador como criterio final y se verifica en pruebas que recorrer todas las páginas devuelve exactamente el total anunciado, sin repeticiones ni ausencias.'),

  h2('7.5 Migraciones automáticas'),
  p('Las bases existentes se actualizan solas al arrancar: se añaden las columnas que falten, se recalculan las claves derivadas y se ajustan los datos heredados, todo ello sin volver a descargar nada y conservando el triaje y las notas. Se verificó sobre una copia de la base real en uso.'),
);

// 8. Limitaciones
hijos.push(
    h1('8. Limitaciones conocidas', true),
  p('Se documentan de forma explícita para que nadie asuma una cobertura que el sistema no tiene.'),
  tabla(
    ['Limitación', 'Detalle'],
    [
      ['Contenido de los PDF de los pliegos', 'La búsqueda cubre título, objeto y descripción de lotes, no el interior de los documentos. Es la mejora que más precisión aportaría y la primera candidata a una segunda fase'],
      ['Filtro territorial en TED', 'TED no expone ningún campo de región utilizable en su API, por lo que sus anuncios aparecen sin comunidad autónoma. Para el resto de fuentes el filtro funciona'],
      ['Comunidades con datos propios en CSV', 'Andalucía, Comunidad Valenciana, Castilla y León, Asturias, Canarias y Aragón publican conjuntos más ricos que no se integran. Lo esencial llega por las plataformas agregadas de PLACSP'],
      ['Avisos por correo o Slack', 'No implementados. La aplicación notifica dentro de su propia interfaz'],
      ['Instancia compartida', 'Cada persona tiene su base de datos y su triaje. No hay estado común entre miembros del equipo'],
      ['Duraciones erróneas en origen', 'En ocasiones el propio comprador publica «36 años» donde quería decir «36 meses». El dato se conserva tal cual viene en lugar de corregirlo'],
    ],
    [30, 70],
  ),
  espacio(),
  p([
    new TextRun({ text: 'Sobre un conector propio para Euskadi. ', bold: true }),
    new TextRun('Se investigó y se descartó con fundamento: su API existe pero no publica el punto de acceso de contratación y todas las rutas probadas devuelven error. El análisis del conjunto de plataformas agregadas de PLACSP mostró que ya cubre País Vasco, Navarra y Galicia con los campos completos, de modo que activarlo resultó gratuito y con menos mantenimiento que escribir tres conectores nuevos.'),
  ]),
);

// 9. Uso
hijos.push(
    h1('9. Instalación y uso', true),
  p('Requisito único: Python 3.9 o superior, que macOS incluye de serie. No hay nada más que instalar.'),
  h2('9.1 Puesta en marcha'),
  p('Doble clic en el fichero start.command, que descarga las novedades y abre la aplicación en el navegador. La primera ejecución tarda algunos minutos; las siguientes, segundos.'),
  h2('9.2 Comandos disponibles'),
  ...codigo([
    'python3 radar.py ingest                       # descarga las novedades',
    'python3 radar.py ingest --backfill 2024,2025  # trae el histórico de esos años',
    'python3 radar.py match                        # reevalúa los perfiles sin descargar',
    'python3 radar.py serve                        # abre la aplicación',
    'python3 radar.py vencimientos --meses 6       # contratos que vencen pronto',
    'python3 radar.py adjudicatarios               # quién gana estos contratos',
    'python3 radar.py export salida.csv            # exporta a CSV',
    'python3 radar.py programar                    # descarga automática cada mañana',
    'python3 radar.py estado                       # cifras y salud de las fuentes',
  ]),
  h2('9.3 Replicabilidad'),
  p('Copiar la carpeta sin el directorio de datos es suficiente para que otra persona la use: al arrancar se crea su propia base y se aplica la configuración de serie. Está verificado como parte de las pruebas, junto con la comprobación de que el proyecto no referencia ningún sistema personal ni requiere paquetes externos.'),
  h2('9.4 Pruebas'),
  ...codigo(['python3 -m unittest discover -s tests -t .']),
  p('Las 126 pruebas incluyen cinco licitaciones reales verificadas —entre ellas la oficina de concienciación de LANTIK, de 915.000 €, y una plataforma de phishing sin CPV— y una veintena de falsos positivos observados que deben seguir quedando fuera: concienciación medioambiental, seguridad vial, prevención de riesgos laborales, vigilantes de seguridad y crema solar de factor 50. Cualquier cambio en el motor de reglas o en los perfiles queda contrastado contra esos casos.'),
);

// --- Documento -------------------------------------------------------------
const doc = new Document({
  creator: 'Ausschreibungsradar',
  title: 'Ausschreibungsradar — Descripción funcional y arquitectura',
  description: 'Descripción de la aplicación, funciones y arquitectura',
  numbering,
  styles: {
    default: {
      document: { run: { font: 'Calibri', size: 22, color: '262626' } },
    },
  },
  features: { updateFields: true },
  sections: [
    {
      properties: {
        page: {
          size: { width: 11906, height: 16838 },  // A4
          margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 },
        },
      },
      footers: {
        default: new Footer({
          children: [
            new Paragraph({
              alignment: AlignmentType.CENTER,
              children: [
                new TextRun({ text: 'Ausschreibungsradar · ', size: 16, color: GRIS }),
                new TextRun({ children: [PageNumber.CURRENT], size: 16, color: GRIS }),
              ],
            }),
          ],
        }),
      },
      children: hijos,
    },
  ],
});

Packer.toBuffer(doc).then((buffer) => {
  fs.writeFileSync(process.argv[2], buffer);
  console.log('escrito ' + process.argv[2]);
});
