/* Language dictionaries for music-organiser.
 *
 * The KEY is the exact English string as it appears in the page. A string with
 * no entry here simply stays in English, so a half-finished language is usable
 * rather than broken, and adding a language is adding one object below.
 *
 * DO NOT translate: product and file names (music-organiser, SPEK-TRO,
 * library.db, web_session.db, config.toml), format names (FLAC, WAV, MP3, CSV,
 * SQL, PNG), service names (Discogs, MusicBrainz, Telegram, Soulseek) or the
 * catalogue data itself. Those are the same words in every language, and
 * "translating" a Spanish release title back into English is worse than
 * leaving the interface in English.
 */
window.I18N_LANGS = {

  /* ── Español ──────────────────────────────────────────────────────────── */
  es: {
    /* header + tabs */
    "music-organiser — click to reload the page": "music-organiser — pulsa para recargar la página",
    "Pipeline": "Proceso",
    "Direct": "Directo",
    "Session": "Sesión",
    "Library": "Biblioteca",
    "Tools": "Herramientas",
    "Telegram": "Telegram",
    "Labels": "Sellos",
    "Full pipeline — import, fetch tags, and organise into the library":
      "Proceso completo: importar, buscar etiquetas y organizar en la biblioteca",
    "Organise straight through, no database":
      "Organiza directamente, sin base de datos",
    "Browse the current import batch before committing it":
      "Revisa el lote importado antes de confirmarlo",
    "Search and browse the full permanent library":
      "Busca y explora la biblioteca permanente completa",
    "Database maintenance, audits, and ad-hoc SQL":
      "Mantenimiento de la base de datos, auditorías y consultas SQL",
    "Control the Telegram scraper and the channel uploader":
      "Controla el rastreador de Telegram y el subidor del canal",
    "Label sorter — compare a record label's Discogs catalogue against the folders you hold, and list what is still missing":
      "Clasificador de sellos: compara el catálogo de Discogs de un sello con las carpetas que tienes y lista lo que aún falta",
    "SPEK-TRO — spectrogram analysis. Finds FLACs that were made from a lossy source (an MP3 re-wrapped as FLAC), and shows you the picture that proves it.":
      "SPEK-TRO — análisis de espectrogramas. Encuentra FLAC creados a partir de una fuente con pérdida (un MP3 reempaquetado como FLAC) y te enseña la imagen que lo demuestra.",
    "Change the language of this page": "Cambia el idioma de esta página",
    "Write Source/Output paths and API keys to config.toml":
      "Guarda las rutas de origen/salida y las claves API en config.toml",
    "Restart the web_ui.py process": "Reinicia el proceso web_ui.py",
    "Save config": "Guardar ajustes",
    "Restart service": "Reiniciar servicio",
    "idle": "en reposo",
    "dismiss": "descartar",
    "Hide until the next reload": "Ocultar hasta la próxima recarga",

    /* pipeline */
    "Input / Output": "Entrada / Salida",
    "Source": "Origen",
    "Output": "Salida",
    "not set": "sin definir",
    "📁 Browse": "📁 Examinar",
    "↗ Open": "↗ Abrir",
    "Click to choose this folder": "Pulsa para elegir esta carpeta",
    "Pick the folder to read from. There is deliberately no default — a path left over from another machine scans nothing and reports success.":
      "Elige la carpeta de la que leer. No hay valor por defecto a propósito: una ruta heredada de otra máquina no analiza nada y aun así informa de éxito.",
    "Opens this folder in the file manager so you can look at the files. Browse PICKS the folder; Open SHOWS you what is in the one already picked.":
      "Abre esta carpeta en el explorador de archivos para que veas los ficheros. Examinar ELIGE la carpeta; Abrir MUESTRA lo que hay en la ya elegida.",
    "Pick the folder to file into. There is deliberately no default.":
      "Elige la carpeta de destino. No hay valor por defecto a propósito.",
    "Scan source": "Analizar origen",
    "Count audio files in the source folder without importing anything":
      "Cuenta los ficheros de audio de la carpeta de origen sin importar nada",
    "Providers &amp; API keys": "Proveedores y claves API",
    "Providers & API keys": "Proveedores y claves API",
    "▶ Run All": "▶ Ejecutar todo",
    "Import": "Importar",
    "Fetch Tags": "Buscar etiquetas",
    "Organise": "Organizar",
    "Clear log": "Limpiar registro",
    "■ Stop": "■ Parar",
    "dry run": "simulación",
    "Import → Fetch Tags → Organise, one after another":
      "Importar → Buscar etiquetas → Organizar, uno tras otro",
    "Copy/move files from Source into the session database":
      "Copia o mueve ficheros del origen a la base de datos de sesión",
    "Look up missing metadata from the enabled providers":
      "Busca los metadatos que faltan en los proveedores activados",
    "Move session files into their final Output location":
      "Mueve los ficheros de la sesión a su ubicación final de salida",
    "Clear the log panel below": "Limpia el panel de registro de abajo",
    "Stop the running job at the next file. Anything already written stays written.":
      "Detiene la tarea en el siguiente fichero. Lo ya escrito se queda escrito.",
    "Preview what would happen without writing or moving anything":
      "Muestra lo que pasaría sin escribir ni mover nada",
    "imported: —": "importados: —",
    "duplicate: —": "duplicados: —",
    "broken: —": "incompletos: —",
    "elapsed: —": "tiempo: —",

    /* direct */
    "Direct mode — input / output, no database":
      "Modo directo — entrada / salida, sin base de datos",
    "Scan only": "Solo analizar",
    "Fetch tags only": "Solo buscar etiquetas",
    "Organise only": "Solo organizar",
    "Count audio files in the source folder without organising anything":
      "Cuenta los ficheros de audio del origen sin organizar nada",
    "All three steps: read the folder, look up what is missing, then move the files into Output":
      "Los tres pasos: leer la carpeta, buscar lo que falta y mover los ficheros a la salida",
    "Read the source folder and report what is there. Writes nothing, moves nothing.":
      "Lee la carpeta de origen e informa de lo que hay. No escribe ni mueve nada.",
    "Look up what is missing and write the tags into the files — but leave every file exactly where it is":
      "Busca lo que falta y escribe las etiquetas en los ficheros, pero deja cada fichero donde está",
    "Move and rename into Output using the tags the files already have — no provider lookups, so no waiting on the internet":
      "Mueve y renombra a la salida con las etiquetas que ya tienen los ficheros: sin consultas a proveedores, sin esperas de red",

    /* session */
    "total": "total",
    "imported": "importados",
    "broken": "incompletos",
    "duplicate": "duplicados",
    "All": "Todos",
    "Imported": "Importados",
    "Broken": "Incompletos",
    "Duplicate": "Duplicados",
    "↺ Refresh": "↺ Recargar",
    "↺ Re-fetch broken": "↺ Reintentar incompletos",
    "↑ Commit to library": "↑ Confirmar en la biblioteca",
    "Show every file in this session": "Muestra todos los ficheros de esta sesión",
    "Show only successfully imported files": "Muestra solo los ficheros importados correctamente",
    "Show files that failed to process": "Muestra los ficheros que no se pudieron procesar",
    "Show files skipped as duplicates": "Muestra los ficheros omitidos por duplicados",
    "Reload from the session database": "Recarga desde la base de datos de sesión",
    "Retry ALL tags for files that came back broken":
      "Reintenta TODAS las etiquetas de los ficheros que quedaron incompletos",
    "Merge this session's files into the permanent library.db":
      "Fusiona los ficheros de esta sesión en la biblioteca permanente library.db",
    "0 selected": "0 seleccionados",
    "How many rows are ticked": "Cuántas filas están marcadas",
    "Select all shown": "Seleccionar todo lo mostrado",
    "Tick every row currently shown by the filter":
      "Marca todas las filas que muestra el filtro",
    "Clear": "Limpiar",
    "Untick everything": "Desmarca todo",
    "⇥ Move to…": "⇥ Mover a…",
    "⧉ Export copy…": "⧉ Exportar copia…",
    "↻ Update from files": "↻ Actualizar desde los ficheros",
    "⭳ Export list (CSV)": "⭳ Exportar lista (CSV)",
    "✕ Remove from session": "✕ Quitar de la sesión",
    "✕ Delete files": "✕ Borrar ficheros",
    "Move the ticked files to a folder you choose. The session's paths follow them.":
      "Mueve los ficheros marcados a la carpeta que elijas. Las rutas de la sesión los siguen.",
    "Copy the ticked files somewhere — export a selection without disturbing the originals":
      "Copia los ficheros marcados a otro sitio: exporta una selección sin tocar los originales",
    "Re-read the tags from the files on disk and update the session. Use after editing tags elsewhere.":
      "Vuelve a leer las etiquetas de los ficheros del disco y actualiza la sesión. Úsalo tras editar etiquetas en otro programa.",
    "Save the ticked rows as a CSV you can open in a spreadsheet":
      "Guarda las filas marcadas como un CSV que puedes abrir en una hoja de cálculo",
    "Remove the ticked rows from the session only. The files themselves are not touched.":
      "Quita las filas marcadas solo de la sesión. Los ficheros no se tocan.",
    "Permanently delete the ticked FILES from disk — cannot be undone":
      "Borra permanentemente del disco los FICHEROS marcados: no se puede deshacer",
    "Tick rows to act on several at once": "Marca filas para actuar sobre varias a la vez",
    "File": "Fichero",
    "Artist": "Artista",
    "Album": "Álbum",
    "Year": "Año",
    "Label": "Sello",
    "Cat#": "Ref.",
    "Dur": "Dur.",
    "Status": "Estado",
    "Title": "Título",
    "Indexed": "Indexados",
    "artists": "artistas",
    "labels": "sellos",
    "files": "ficheros",

    /* library */
    "Rows in library.db — one per audio file":
      "Filas en library.db: una por fichero de audio",
    "Distinct artist tags across those files":
      "Etiquetas de artista distintas entre esos ficheros",
    "Distinct record labels — only as good as the tags":
      "Sellos distintos: valen lo que valgan las etiquetas",
    "Total size of the files these rows point at":
      "Tamaño total de los ficheros a los que apuntan estas filas",
    "Matches artist, album, title and label. Plain text, no wildcards needed — typing part of a word is enough.":
      "Busca en artista, álbum, título y sello. Texto normal, sin comodines: basta con parte de una palabra.",
    "Every row, whatever its state": "Todas las filas, sea cual sea su estado",
    "Files this app moved into the output folder and tagged. The normal, finished state.":
      "Ficheros que esta aplicación movió a la carpeta de salida y etiquetó. El estado normal y terminado.",
    "Files found by Rebuild index — they are on disk and catalogued, but never went through Import, so they were not renamed or re-tagged.":
      "Ficheros encontrados por Reconstruir índice: están en el disco y catalogados, pero nunca pasaron por Importar, así que no se renombraron ni se reetiquetaron.",
    "Missing artist, album or title. It means the TAGS are incomplete — it says nothing about the audio, which usually plays perfectly. Tools → Tags from filenames often fixes these.":
      "Falta el artista, el álbum o el título. Significa que las ETIQUETAS están incompletas; no dice nada del audio, que normalmente suena perfectamente. Herramientas → Etiquetas desde el nombre suele arreglarlos.",
    "Re-read library.db. Use it after an import or a rebuild.":
      "Vuelve a leer library.db. Úsalo tras una importación o una reconstrucción.",
    "The artist tag on the file. Blank means the tag is missing, not that the artist is unknown.":
      "La etiqueta de artista del fichero. En blanco significa que falta la etiqueta, no que se desconozca el artista.",
    "The album tag. A loose single legitimately has none.":
      "La etiqueta de álbum. Un single suelto legítimamente no tiene ninguna.",
    "The track title tag": "La etiqueta de título del corte",
    "Release year, from the date tag": "Año de edición, de la etiqueta de fecha",
    "Record label, from the label/publisher tag":
      "Sello discográfico, de la etiqueta de sello o editor",
    "Catalogue number — the label's own reference for the release. The most reliable thing to match a pressing on.":
      "Número de catálogo: la referencia propia del sello para la edición. Lo más fiable para identificar una prensa.",
    "imported = filed by this app · indexed = found on disk, never imported · broken = a required tag is missing · duplicate = the same audio is already in the library":
      "importado = archivado por esta aplicación · indexado = encontrado en el disco, nunca importado · incompleto = falta una etiqueta obligatoria · duplicado = ese mismo audio ya está en la biblioteca",
    "← Prev": "← Anterior",
    "Next →": "Siguiente →",
    "page 1 of 1": "página 1 de 1",
    "Previous page": "Página anterior",
    "Next page": "Página siguiente",

    /* tools */
    "Database": "Base de datos",
    "Rebuild index": "Reconstruir índice",
    "re-scan output → library.db": "volver a analizar la salida → library.db",
    "Compact DB": "Compactar BD",
    "VACUUM + ANALYZE library.db": "VACUUM + ANALYZE sobre library.db",
    "Commit session → library": "Confirmar sesión → biblioteca",
    "merge session DB into library.db": "fusiona la BD de sesión en library.db",
    "Walk the Output folder and rebuild library.db from what's actually on disk":
      "Recorre la carpeta de salida y reconstruye library.db a partir de lo que hay realmente en el disco",
    "Reclaim disk space and rebuild query statistics on library.db":
      "Recupera espacio en disco y rehace las estadísticas de consulta de library.db",
    "Fix &amp; Rescue": "Arreglar y rescatar",
    "Fix & Rescue": "Arreglar y rescatar",
    "Tags from filenames": "Etiquetas desde el nombre",
    "fills artist/title where the name already says it":
      "rellena artista/título cuando el nombre ya lo dice",
    "Tags from filenames — preview": "Etiquetas desde el nombre — vista previa",
    "dry run, writes nothing": "simulación, no escribe nada",
    "Re-fetch broken": "Reintentar incompletos",
    "retry ALL tags for session files":
      "reintenta TODAS las etiquetas de los ficheros de la sesión",
    "Re-organise session": "Reorganizar la sesión",
    "move files to updated paths": "mueve los ficheros a sus rutas actualizadas",
    "Audit library.db": "Auditar library.db",
    "Run all checks": "Ejecutar todas las comprobaciones",
    "13 quality checks on library.db": "13 comprobaciones de calidad sobre library.db",
    "Run all 13 data-quality checks against library.db":
      "Ejecuta las 13 comprobaciones de calidad de datos sobre library.db",
    "&quot;Broken&quot; means missing artist/album/title tags — never damaged audio. This reads those names off the filename (01. Artist - Title) and writes them in.":
      "«Incompleto» significa que faltan las etiquetas de artista/álbum/título, nunca que el audio esté dañado. Esto lee esos nombres del nombre del fichero (01. Artista - Título) y los escribe.",
    "Show exactly what would be written, change nothing":
      "Muestra exactamente lo que se escribiría, sin cambiar nada",
    "Retry metadata lookup for every file in the session, not just the broken ones":
      "Reintenta la búsqueda de metadatos para todos los ficheros de la sesión, no solo los incompletos",
    "Re-run the Organise step so files land at their current (possibly updated) target paths":
      "Vuelve a ejecutar Organizar para que los ficheros acaben en sus rutas de destino actuales",
    "Naming &amp; filing": "Nombres y archivado",
    "Naming & filing": "Nombres y archivado",
    "How Organise names the folders it moves your files into. The examples are produced by the same code that does the moving, so what you see here is what you get.":
      "Cómo nombra Organizar las carpetas a las que mueve tus ficheros. Los ejemplos los genera el mismo código que hace el movimiento, así que lo que ves aquí es lo que obtienes.",
    "When importing": "Al importar",
    "copy (leave the originals)": "copiar (deja los originales)",
    "move (take the originals)": "mover (se lleva los originales)",
    "Delete leftover extras": "Borrar los extras sobrantes",
    "COPY leaves the source untouched — safer, uses twice the space. MOVE takes the files out of the source folder. If the source is a torrent you are seeding, move will break the seed.":
      "COPIAR deja el origen intacto: más seguro, ocupa el doble. MOVER saca los ficheros de la carpeta de origen. Si el origen es un torrent que estás compartiendo, mover romperá el seed.",
    "Artwork, logs, cue sheets and nfos left behind once the audio has been filed. On = they are deleted; off = they are gathered into the orphan folder.":
      "Carátulas, logs, cue sheets y nfos que quedan tras archivar el audio. Activado = se borran; desactivado = se reúnen en la carpeta de huérfanos.",
    "SQL Query": "Consulta SQL",
    "▶ Run": "▶ Ejecutar",
    "example": "ejemplo",
    "Run a SELECT query above to see results here.":
      "Ejecuta arriba una consulta SELECT para ver los resultados aquí.",
    "Run the SELECT/WITH query above against the chosen database":
      "Ejecuta la consulta SELECT/WITH de arriba sobre la base de datos elegida",
    "Fill the box with a sample query": "Rellena el cuadro con una consulta de ejemplo",

    /* labels tab */
    "no catalogue yet": "aún no hay catálogo",
    "Change label…": "Cambiar de sello…",
    "Get catalogue": "Obtener catálogo",
    "Get tracklists": "Obtener listas de cortes",
    "Search Discogs for a different record label and make it the one this tab reports on":
      "Busca otro sello en Discogs y conviértelo en el que analiza esta pestaña",
    "Download this label's full release list from Discogs. Reuses the copy label2lossless already fetched when there is one, so it usually costs nothing.":
      "Descarga de Discogs la lista completa de ediciones de este sello. Reutiliza la copia que ya bajó label2lossless si existe, así que normalmente no cuesta nada.",
    "Fetch the track listings for releases that still lack one — that is what turns 'we have this' into 'we have 9 of its 12 tracks'. One Discogs call per release, so it runs in capped batches and can be run again.":
      "Descarga las listas de cortes de las ediciones que aún no la tienen: es lo que convierte «tenemos esto» en «tenemos 9 de sus 12 cortes». Una llamada a Discogs por edición, así que va por lotes limitados y se puede repetir.",
    "Folders to compare": "Carpetas a comparar",
    "Add the folders that hold this label.": "Añade las carpetas que contienen este sello.",
    "Owned": "Propias",
    "is what you already have;": "es lo que ya tienes;",
    "Incoming": "Entrantes",
    "is a share or download being judged against it.":
      "es una carpeta compartida o descargada que se compara con ellas.",
    "+ Owned folder": "+ Carpeta propia",
    "+ Incoming": "+ Entrante",
    "Add a folder you already own. Nothing is written to it — it is read to work out what you hold.":
      "Añade una carpeta que ya tengas. No se escribe nada en ella: se lee para averiguar qué posees.",
    "Add a folder you are judging: a shared folder, a download, a friend's drive. Each folder in it is reported as new, an upgrade, or a duplicate of what you own.":
      "Añade una carpeta que estás evaluando: una carpeta compartida, una descarga, el disco de un amigo. Cada carpeta se informa como nueva, mejora o duplicado de lo que ya tienes.",
    "Archive (where Move files things)": "Archivo (a donde Mover lleva las cosas)",
    "The folder Move puts releases into. Left unset on purpose — a path from another machine would move files somewhere you cannot see.":
      "La carpeta donde Mover deja las ediciones. Sin definir a propósito: una ruta de otra máquina movería ficheros a un sitio que no puedes ver.",
    "Choose the folder that Move files releases into":
      "Elige la carpeta a la que Mover archiva las ediciones",
    "Run": "Ejecutar",
    "Scan folders": "Analizar carpetas",
    "Read every folder you added, match each one to the catalogue, and work out what is complete, partial and missing. The first run reads tags off every file; after that only folders that changed are re-read.":
      "Lee cada carpeta que has añadido, la empareja con el catálogo y calcula qué está completo, incompleto o ausente. La primera pasada lee las etiquetas de todos los ficheros; después solo se releen las carpetas que han cambiado.",
    "Stop the running job at the next folder":
      "Detiene la tarea en la siguiente carpeta",
    "Catalogue": "Catálogo",
    "Not in catalogue": "Fuera del catálogo",
    "Move log": "Registro de movimientos",
    "Every release on this label, with what you hold of it":
      "Todas las ediciones de este sello, con lo que tienes de cada una",
    "Folders from your Incoming roots, judged against what you own: new, an upgrade, or a duplicate":
      "Carpetas de tus orígenes entrantes, comparadas con lo que ya tienes: nueva, mejora o duplicado",
    "Folders under your roots that this label's catalogue does not contain — usually a different label, or a folder named so the catalogue number cannot be read":
      "Carpetas dentro de tus orígenes que no están en el catálogo de este sello: normalmente son de otro sello, o su nombre no deja leer el número de catálogo",
    "Every folder this tab has moved, newest first, each one with a button to put it back":
      "Todas las carpetas que ha movido esta pestaña, de la más reciente a la más antigua, cada una con un botón para devolverla",
    "Missing": "Ausentes",
    "Incomplete": "Incompletas",
    "Complete": "Completas",
    "Unverified": "Sin verificar",
    "Show every release": "Muestra todas las ediciones",
    "Releases on the label that you do not hold at all":
      "Ediciones del sello que no tienes en absoluto",
    "Releases you hold only part of — these are where the outstanding tracks come from":
      "Ediciones de las que solo tienes una parte: de aquí salen los cortes pendientes",
    "Releases you hold in full": "Ediciones que tienes completas",
    "You have a folder for it, but Discogs has no track listing cached yet, so completeness is unknown":
      "Tienes una carpeta, pero todavía no hay lista de cortes de Discogs guardada, así que no se sabe si está completa",
    "Filter the rows below": "Filtra las filas de abajo",
    "search catalogue number, artist or title…":
      "buscar número de catálogo, artista o título…",
    "Select all": "Seleccionar todo",
    "Tick every row shown": "Marca todas las filas mostradas",
    "Preview move": "Simular movimiento",
    "Move to archive": "Mover al archivo",
    "Show exactly what would move, and where, without touching a file":
      "Muestra exactamente qué se movería y a dónde, sin tocar ningún fichero",
    "Move the ticked folders into the Archive folder. Every move is written to a log with a button to put it back.":
      "Mueve las carpetas marcadas a la carpeta de archivo. Cada movimiento se anota en un registro con un botón para deshacerlo.",
    "Export:": "Exportar:",
    "Outstanding tracks": "Cortes pendientes",
    "Missing releases (CSV)": "Ediciones ausentes (CSV)",
    "Incomplete (CSV)": "Incompletas (CSV)",
    "Search terms": "Términos de búsqueda",
    "What we have (CSV)": "Lo que tenemos (CSV)",
    "A readable list of every track still missing, grouped under its release — the list you actually hunt from":
      "Una lista legible de cada corte que falta, agrupado por su edición: la lista con la que realmente se busca",
    "Spreadsheet of releases you do not hold at all":
      "Hoja de cálculo con las ediciones que no tienes en absoluto",
    "Spreadsheet of the releases you hold only part of, with the missing track names":
      "Hoja de cálculo de las ediciones incompletas, con los nombres de los cortes que faltan",
    "One search line per thing to look for, as artist + title. Never title alone — a bare title matches the wrong release.":
      "Una línea de búsqueda por cada cosa que buscar, como artista + título. Nunca solo el título: un título suelto encuentra la edición equivocada.",
    "Spreadsheet of everything you hold on this label":
      "Hoja de cálculo con todo lo que tienes de este sello",
    "Nothing scanned yet. Add a folder on the left, then press":
      "Aún no se ha analizado nada. Añade una carpeta a la izquierda y pulsa",
    "Job log": "Registro de la tarea",
    "Clear the log below": "Limpia el registro de abajo",

    /* file browser */
    "Choose a folder": "Elige una carpeta",
    "Close without choosing (Esc)": "Cerrar sin elegir (Esc)",
    "Type or paste a path and press Enter": "Escribe o pega una ruta y pulsa Intro",
    "Go": "Ir",
    "Go to the typed path": "Ir a la ruta escrita",
    "▲ Up": "▲ Subir",
    "Up one level": "Subir un nivel",
    "Shortcuts on this machine": "Accesos directos de esta máquina",
    "Selected": "Seleccionada",
    "Cancel": "Cancelar",
    "Use this folder": "Usar esta carpeta",
    "Use the folder shown above": "Usa la carpeta mostrada arriba",
    "Close": "Cerrar",

    /* spek-tro */
    "Folder": "Carpeta",
    "picked folder": "carpeta elegida",
    "force re-check": "forzar recomprobación",
    "▶ Scan for fake FLACs": "▶ Buscar FLAC falsos",
    "⚛ Vamp-confirm suspects": "⚛ Confirmar sospechosos con Vamp",
    "⭳ Save spectrograms (.zip)": "⭳ Guardar espectrogramas (.zip)",
    "↗ Share": "↗ Compartir",
    "✓ Dismiss": "✓ Descartar",
    "⇥ Isolate": "⇥ Aislar",
    "✕ Delete": "✕ Borrar",
    "✕✕ Delete all suspects": "✕✕ Borrar todos los sospechosos",
    "Artist / Album": "Artista / Álbum",
    "Verdict": "Veredicto",
    "Cutoff": "Corte",
    "Wall": "Muro",
    "Above": "Encima",
    "Confidence": "Confianza",
    "Why": "Por qué",
    "Spectrogram": "Espectrograma",
    "⭳ Save PNG": "⭳ Guardar PNG",
    "⭳ Save picture": "⭳ Guardar imagen",
    "⎘ Copy link": "⎘ Copiar enlace",
    "✓ Dismiss (false positive)": "✓ Descartar (falso positivo)",
    "File detail": "Detalle del fichero",
    "Rendering spectrogram…": "Generando espectrograma…",
    "checking availability…": "comprobando disponibilidad…",
    "checking…": "comprobando…",
    "Running…": "Ejecutando…",
    "not set — pick a folder to check it directly, no import needed":
      "sin definir: elige una carpeta para comprobarla directamente, sin importar nada",
    "Click Browse, or pick from library.db/session.db below instead":
      "Pulsa Examinar, o elige abajo entre library.db y session.db",
    "Choose a folder to scan directly — bypasses the database entirely":
      "Elige una carpeta para analizarla directamente: se salta la base de datos por completo",
    "Which set of files to scan/browse": "Qué conjunto de ficheros analizar o explorar",
    "Re-check files that were already scanned, instead of skipping them":
      "Vuelve a comprobar los ficheros ya analizados en vez de omitirlos",
    "FFT spectral-cutoff scan — flags FLACs that look transcoded from a lossy source":
      "Análisis FFT del corte espectral: marca los FLAC que parecen transcodificados de una fuente con pérdida",
    "Reload the suspects list": "Recarga la lista de sospechosos",
    "Tick every suspect on this page": "Marca todos los sospechosos de esta página",
    "Render the ticked files' spectrograms and download them as one .zip":
      "Genera los espectrogramas de los ficheros marcados y los descarga en un único .zip",
    "Hand the spectrograms to another app — Signal, Telegram, mail. Falls back to a download where the browser has no share support.":
      "Pasa los espectrogramas a otra aplicación: Signal, Telegram, correo. Si el navegador no sabe compartir, los descarga.",
    "False positives — clear the flag on the ticked files, leave them alone":
      "Falsos positivos: quita la marca de los ficheros seleccionados y déjalos en paz",
    "Move the ticked files into the Suspected Transcodes folder":
      "Mueve los ficheros marcados a la carpeta de transcodificaciones sospechosas",
    "Permanently delete the ticked files — cannot be undone":
      "Borra permanentemente los ficheros marcados: no se puede deshacer",
    "Permanently delete EVERY suspect in this database, not just this page — cannot be undone":
      "Borra permanentemente TODOS los sospechosos de esta base de datos, no solo los de esta página: no se puede deshacer",
    "The file on disk": "El fichero en el disco",
    "Tags read from the file": "Etiquetas leídas del fichero",
    "lossy = made from an MP3 · padded = 16-bit audio stored as 24-bit · upsampled = a low-rate master stretched to a high sample rate · suspect = one signal only, look at the picture · clean = nothing found":
      "con pérdida = hecho a partir de un MP3 · rellenado = audio de 16 bits guardado como 24 · sobremuestreado = un máster de baja frecuencia estirado a una alta · sospechoso = una sola señal, mira la imagen · limpio = no se ha encontrado nada",
    "Where the sound stops. Real lossless runs close to Nyquist (about 22 kHz on a 44.1 kHz file); a wall well below that is an encoder's fingerprint.":
      "Dónde se acaba el sonido. Un sin pérdida real llega cerca de Nyquist (unos 22 kHz en un fichero de 44,1 kHz); un muro muy por debajo es la huella de un codificador.",
    "How sharply it stops — the dB drop across the cutoff. A mastering filter slopes; an encoder puts a brick wall there (20 dB or more).":
      "Con qué brusquedad se acaba: la caída en dB en el corte. Un filtro de masterización cae en pendiente; un codificador pone un muro (20 dB o más).",
    "How much energy is left ABOVE the cutoff. Real 16-bit masters keep a dither floor up there; an encoder leaves it dead (-90 dB or lower).":
      "Cuánta energía queda POR ENCIMA del corte. Un máster real de 16 bits mantiene ahí un suelo de dither; un codificador lo deja muerto (-90 dB o menos).",
    "How sure the detector is that this is LOSSY. 0% means it found nothing wrong.":
      "Cuánta seguridad tiene el detector de que esto tiene PÉRDIDA. 0 % significa que no encontró nada raro.",
    "Which numbers made the call": "Qué cifras han decidido el veredicto",
    "Save this picture as a PNG": "Guarda esta imagen como PNG",
    "Download this spectrogram as a PNG file":
      "Descarga este espectrograma como fichero PNG",
    "Copy a direct link to this spectrogram image, for anyone else on the LAN":
      "Copia un enlace directo a esta imagen del espectrograma, para cualquiera de la red local",
    "Hand this picture to another app — Signal, Telegram, mail":
      "Pasa esta imagen a otra aplicación: Signal, Telegram, correo",
    "Opens the containing folder in the file manager, with this file in it.":
      "Abre la carpeta que lo contiene en el explorador de archivos, con este fichero dentro.",
    "False positive — clear the flag, leave the file alone":
      "Falso positivo: quita la marca y deja el fichero en paz",
    "False positive — clear the suspected flag, leave the file where it is":
      "Falso positivo: quita la marca de sospecha y deja el fichero donde está",
    "Move it into the Suspected Transcodes folder":
      "Muévelo a la carpeta de transcodificaciones sospechosas",
    "Move this file into the Suspected Transcodes folder under Output":
      "Mueve este fichero a la carpeta de transcodificaciones sospechosas dentro de la salida",
    "Permanently delete this file — cannot be undone":
      "Borra permanentemente este fichero: no se puede deshacer",
    "Close the picture": "Cierra la imagen",
    "Time left→right, frequency bottom→top, brightness = energy. A hard ceiling well below the top means a lossy source.":
      "El tiempo va de izquierda a derecha, la frecuencia de abajo arriba, el brillo es energía. Un techo duro muy por debajo del borde superior indica una fuente con pérdida.",
    "Time runs left to right, frequency bottom to top, brightness is energy.":
      "El tiempo va de izquierda a derecha, la frecuencia de abajo arriba, el brillo es la energía.",
    "A lossy ancestor shows as a dead band with a razor-straight edge across the top.":
      "Un origen con pérdida aparece como una banda muerta con un borde recto como una cuchilla en la parte de arriba.",
    "A genuine master fades out unevenly and keeps noise all the way to the top of the picture. An old or deliberately dull master can look band-limited too — that is why a low confidence is worth a look before you delete anything.":
      "Un máster auténtico se desvanece de forma irregular y conserva ruido hasta lo más alto de la imagen. Un máster antiguo o deliberadamente apagado también puede parecer limitado en banda: por eso conviene mirar los de confianza baja antes de borrar nada.",

    /* telegram */
    "Channel uploader": "Subidor del canal",
    "Posting to the channel": "Publicando en el canal",
    "Turning this off never kills a release mid-flight — state is only saved once every file lands.":
      "Apagar esto nunca corta una edición a medias: el estado solo se guarda cuando han llegado todos los ficheros.",
    "Off writes the STOP latch; the run finishes its current release, then exits":
      "Apagado escribe el cierre STOP; la ejecución termina la edición en curso y sale",
    "Posted": "Publicadas",
    "Files": "Ficheros",
    "Failed": "Fallidas",
    "Held": "Retenidas",
    "Held back — needs a human": "Retenidas: necesitan una persona",
    "Folders too big to be one release — a container or a whole series dumped flat. Not posted, not marked done.":
      "Carpetas demasiado grandes para ser una sola edición: un contenedor o una serie entera volcada sin separar. Ni se publican ni se marcan como hechas.",
    "Each of these resolved to more tracks than one release can honestly hold. Split it into per-release folders (or per-CD ones) and it posts on the next pass — nothing here is lost, and nothing is marked done.":
      "Cada una de estas da más cortes de los que una sola edición puede tener honestamente. Divídela en carpetas por edición (o por CD) y se publicará en la siguiente pasada: aquí no se pierde nada ni se marca nada como hecho.",
    "Retry failed": "Reintentar las fallidas",
    "Clear the failed list so the next run retries those releases":
      "Vacía la lista de fallidas para que la siguiente ejecución reintente esas ediciones",
    "↗ Open folder": "↗ Abrir carpeta",
    "Uploader settings": "Ajustes del subidor",
    "Upload connections": "Conexiones de subida",
    "Parallel connections per file. Measured on this box with Premium: 1 conn 1.47 MB/s, 4 conns 4.35, 8 conns 4.80, 16 conns 4.87.":
      "Conexiones en paralelo por fichero. Medido en esta máquina con Premium: 1 conexión 1,47 MB/s, 4 conexiones 4,35, 8 conexiones 4,80, 16 conexiones 4,87.",
    "Repack WAV → FLAC": "Reempaquetar WAV → FLAC",
    "Send a FLAC instead of the WAV. Lossless, about a third smaller, ~1s of CPU. The library file is never touched.":
      "Envía un FLAC en lugar del WAV. Sin pérdida, alrededor de un tercio más pequeño, ~1 s de CPU. El fichero de la biblioteca no se toca nunca.",
    "Reuse duplicate audio": "Reutilizar audio duplicado",
    "If the same audio was already sent, copy that message server-side instead of uploading the bytes again.":
      "Si ese mismo audio ya se envió, copia aquel mensaje en el servidor en vez de volver a subir los bytes.",
    "Max tracks per release": "Máx. cortes por edición",
    "A folder resolving to more tracks than this is a container or a series dumped flat: held for review, never posted.":
      "Una carpeta que dé más cortes que esto es un contenedor o una serie volcada sin separar: se retiene para revisión y nunca se publica.",
    "Marker share": "Proporción de marcadores",
    "Share of a release's track titles that must carry a bases/vocal marker before it is filed under that tab.":
      "Proporción de títulos de una edición que deben llevar marca de bases/vocal para archivarla en esa pestaña.",
    "NEW from year": "NUEVO desde el año",
    "Releases from this year go to the NEW tab first, genre second. 0 turns it off.":
      "Las ediciones de este año van primero a la pestaña NUEVO y luego al género. 0 lo desactiva.",
    "Messages / minute": "Mensajes / minuto",
    "Messages per minute. ~20/min per group ran clean with zero flood waits.":
      "Mensajes por minuto. Unos 20/min por grupo funcionaron sin ninguna espera por saturación.",
    "Pause between releases": "Pausa entre ediciones",
    "Seconds between releases.": "Segundos entre ediciones.",
    "Root": "Raíz",
    "Which library root to upload, by a substring of its folder name.":
      "Qué raíz de la biblioteca subir, indicando parte del nombre de su carpeta.",
    "Quiet hours": "Horas de silencio",
    "Hours to stay off the line, as HH-HH (e.g. 09-17). Empty = upload any time.":
      "Horas en las que no usar la línea, como HH-HH (p. ej. 09-17). Vacío = subir a cualquier hora.",
    "Check authenticity": "Comprobar autenticidad",
    "Run the transcode detector before posting. A release whose audio is confidently an MP3 in a FLAC wrapper is held back and listed in lossy_review.txt. Nothing is moved or deleted.":
      "Ejecuta el detector de transcodificaciones antes de publicar. Una edición cuyo audio sea con seguridad un MP3 envuelto en FLAC se retiene y se anota en lossy_review.txt. No se mueve ni se borra nada.",
    "Lossy confidence": "Confianza de pérdida",
    "How sure the detector must be before a release is held. 0.90 = a hard cutoff below 16.5 kHz. Lower values also catch 160-256 kbps sources, at the cost of flagging vinyl rips and band-limited masters.":
      "Cuánta seguridad debe tener el detector para retener una edición. 0,90 = un corte duro por debajo de 16,5 kHz. Valores menores también pillan fuentes de 160-256 kbps, a costa de marcar rips de vinilo y másteres limitados en banda.",
    "Telegram tools": "Herramientas de Telegram",
    "Checks that otherwise only announce themselves by an upload failing: am I still allowed to post, is the group still a forum, does the topic exist, is the account flood-limited.":
      "Comprobaciones que si no solo se manifiestan cuando falla una subida: ¿sigo teniendo permiso para publicar?, ¿el grupo sigue siendo un foro?, ¿existe el tema?, ¿la cuenta está limitada por saturación?",
    "Account": "Cuenta",
    "Permissions": "Permisos",
    "Topics": "Temas",
    "Username, id, Premium, home DC and the per-file size limit":
      "Usuario, id, Premium, centro de datos y límite de tamaño por fichero",
    "Admin rights, forum status, slow mode, content protection - and what would block an upload":
      "Permisos de administración, estado de foro, modo lento, protección de contenido y qué impediría una subida",
    "Every topic with its message count and last activity":
      "Todos los temas con su número de mensajes y su última actividad",
    "Ask @SpamBot directly whether the account is limited":
      "Pregunta directamente a @SpamBot si la cuenta está limitada",
    "Jobs. The ones marked (stop first) edit the state file or the session a running upload is holding.":
      "Tareas. Las marcadas con (parar antes) modifican el fichero de estado o la sesión que tiene abierta una subida en curso.",
    "Report": "Informe",
    "Decide tabs": "Decidir pestañas",
    "Decide posted": "Decidir publicadas",
    "Re-home: check": "Recolocar: comprobar",
    "Re-home: apply": "Recolocar: aplicar",
    "Seed dedupe map": "Sembrar mapa de duplicados",
    "Verify": "Verificar",
    "Count what would be posted per tab, post nothing":
      "Cuenta lo que se publicaría en cada pestaña, sin publicar nada",
    "Read every release's tags and settle its tab up front (about an hour)":
      "Lee las etiquetas de cada edición y decide su pestaña de antemano (alrededor de una hora)",
    "(stop first) Decide tabs for the releases already posted - the short pass":
      "(parar antes) Decide las pestañas de las ediciones ya publicadas: la pasada corta",
    "(stop first) List already-posted releases sitting in the wrong tab. Changes nothing.":
      "(parar antes) Lista las ediciones ya publicadas que están en la pestaña equivocada. No cambia nada.",
    "(stop first) DELETE those posts and re-queue them for the right tab":
      "(parar antes) BORRA esas publicaciones y las vuelve a encolar en la pestaña correcta",
    "(stop first) Record what is already posted so a duplicate is copied, never re-uploaded":
      "(parar antes) Anota lo ya publicado para que un duplicado se copie y nunca se vuelva a subir",
    "(stop first) Reconcile the group against the state file: what is marked posted but is not there":
      "(parar antes) Contrasta el grupo con el fichero de estado: qué figura como publicado pero no está",
    "Channel scraper": "Rastreador de canales",
    "Channel": "Canal",
    "id, @username, or part of the name": "id, @usuario o parte del nombre",
    "Ambiguous names print the candidates instead of guessing — mining the wrong chat costs an evening.":
      "Los nombres ambiguos muestran los candidatos en vez de adivinar: rastrear el chat equivocado cuesta una tarde.",
    "Dry run": "Simulación",
    "Oldest first": "Más antiguos primero",
    "Archives": "Archivos comprimidos",
    "Limit": "Límite",
    "Max GB": "GB máx.",
    "List channels": "Listar canales",
    "Scan": "Analizar",
    "Download": "Descargar",
    "Stop": "Parar",
    "Pick a channel and scan it.": "Elige un canal y analízalo.",
    "List exactly what would be fetched, take nothing":
      "Lista exactamente lo que se descargaría, sin descargar nada",
    "Oldest messages first (default is newest first)":
      "Mensajes más antiguos primero (por defecto, los más nuevos primero)",
    "Also fetch archives whose NAME says lossless":
      "Descarga también los archivos comprimidos cuyo NOMBRE diga «lossless»",

    /* strings app.js builds at runtime. They are emitted as their own text
       nodes, with the numbers kept outside, so an exact key can still match. */
    "releases": "ediciones",
    "with track listings": "con lista de cortes",
    "no catalogue yet — press Get catalogue":
      "aún no hay catálogo: pulsa Obtener catálogo",
    "no Discogs token — add one in the Pipeline tab":
      "sin token de Discogs: añade uno en la pestaña Proceso",
    "OWNED": "PROPIA",
    "INCOMING": "ENTRANTE",
    "missing": "falta",
    "last scan": "último análisis",
    "never scanned": "nunca analizado",
    "showing": "mostrando",
    "No folders yet. Add at least one.": "Aún no hay carpetas. Añade al menos una.",
    "This folder is not there right now — an unreadable root scans nothing and would otherwise report success":
      "Esta carpeta no está ahora mismo: un origen ilegible no analiza nada y por lo demás informaría de éxito",
    "nothing scanned yet": "aún no se ha analizado nada",
    "Nothing has been moved yet.": "Todavía no se ha movido nada.",
    "Every folder matched a release in this catalogue.":
      "Todas las carpetas coinciden con una edición de este catálogo.",
    "Nothing matches that filter.": "Nada coincide con ese filtro.",
    "lossless": "sin pérdida",
    "lossy": "con pérdida",
    "Put back": "Devolver",
    "Move this folder back where it came from":
      "Devuelve esta carpeta al sitio de donde vino",
    "Show the tracks still missing from this release":
      "Muestra los cortes que aún faltan de esta edición",
    "Open the folder you hold in the file manager":
      "Abre en el explorador de archivos la carpeta que tienes",
    "Open this folder in the file manager":
      "Abre esta carpeta en el explorador de archivos",
    "Stop using this folder. Nothing on disk is touched.":
      "Deja de usar esta carpeta. No se toca nada del disco.",
    /* summary tiles */
    "in catalogue": "en el catálogo",
    "complete": "completas",
    "incomplete": "incompletas",
    "unverified": "sin verificar",
    "tracks outstanding": "cortes pendientes",
    "to convert": "por convertir",
    "new from incoming": "nuevas de entrantes",
    "upgrades": "mejoras",
    "duplicates": "duplicados",
    "new": "nueva",
    "upgrade": "mejora",
    "Every release Discogs files under this label":
      "Todas las ediciones que Discogs archiva bajo este sello",
    "Releases you hold only part of": "Ediciones de las que solo tienes una parte",
    "Releases you do not hold at all": "Ediciones que no tienes en absoluto",
    "You have a folder but no track listing to check it against":
      "Tienes una carpeta pero ninguna lista de cortes con la que comprobarla",
    "Individual tracks still to find, across every release":
      "Cortes sueltos que aún hay que encontrar, en todas las ediciones",
    "Releases whose best folder contains WAV/AIFF rather than FLAC":
      "Ediciones cuya mejor carpeta contiene WAV/AIFF en vez de FLAC",
    "Incoming folders for releases you do not own":
      "Carpetas entrantes de ediciones que no tienes",
    "Incoming folders more complete than the one you own":
      "Carpetas entrantes más completas que la que ya tienes",
    "Incoming folders that add nothing": "Carpetas entrantes que no aportan nada",
    "Every track Discogs lists for this release is in your folder":
      "Todos los cortes que Discogs lista para esta edición están en tu carpeta",
    "Your folder is missing some of the tracks Discogs lists":
      "A tu carpeta le faltan algunos de los cortes que lista Discogs",
    "You do not hold this release at all": "No tienes esta edición en absoluto",
    "An incoming folder for a release you do not own — this is a gain":
      "Una carpeta entrante de una edición que no tienes: esto es una ganancia",
    "The incoming folder holds more of this release than the one you own":
      "La carpeta entrante tiene más de esta edición que la que ya tienes",
    "The incoming folder adds nothing you do not already have":
      "La carpeta entrante no aporta nada que no tengas ya",
    "No Discogs track listing cached for this release, so completeness cannot be checked. Press Get tracklists.":
      "No hay lista de cortes de Discogs guardada para esta edición, así que no se puede comprobar si está completa. Pulsa Obtener listas de cortes.",
    "The only audio in that folder is lossy. It does not count as owning the release.":
      "El único audio de esa carpeta es con pérdida. No cuenta como tener la edición.",

    /* misc placeholders */
    "filter artist / album / title…": "filtrar artista / álbum / título…",
    "search artist / album / title / label…":
      "buscar artista / álbum / título / sello…",
    "e.g. 09-17": "p. ej. 09-17",
    "GB": "GB"
  }
};
