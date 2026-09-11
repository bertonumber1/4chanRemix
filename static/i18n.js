/* Language switching for music-organiser.
 *
 * THE ENGLISH STRING IS THE KEY.  There are no `data-i18n="some.dotted.key"`
 * attributes to add and keep in step: the page is written in English, and a
 * language is a dictionary from those exact English strings to the other
 * language.  Three things follow from that, all of them wanted:
 *
 *   - nothing has to be annotated, so a new button is translatable the moment
 *     someone adds a line to a dictionary, and NOT translating it just leaves
 *     English on screen instead of a raw key like "tab.labels.title";
 *   - text that app.js builds at runtime is covered by the same pass, because
 *     a MutationObserver re-translates whatever gets inserted;
 *   - a missing translation is invisible rather than broken.
 *
 * What must NEVER be translated is DATA: folder names, catalogue numbers, log
 * lines, track titles, SQL. Those live inside [data-i18n-skip] subtrees, and
 * the walker refuses to enter them. Getting this wrong would "translate" a
 * Spanish release title into English, which is worse than doing nothing.
 *
 * The original English is remembered per node, so switching es -> en -> de
 * works; without that, the second switch would be looking up a Spanish key.
 */
(function () {
  "use strict";

  var STORE = "mo-lang";
  var ATTRS = ["title", "placeholder", "aria-label", "alt"];

  var origText = new WeakMap();   // text node -> original English
  var current = "en";
  var observer = null;
  var pending = null;

  function dict() {
    return (window.I18N_LANGS && window.I18N_LANGS[current]) || null;
  }

  /* Translate one string, keeping the whitespace that surrounded it. */
  function tr(raw, d) {
    var key = raw.trim();
    if (!key) return raw;
    var hit = d[key];
    if (hit === undefined) return raw;
    return raw.replace(key, hit);
  }

  function skip(el) {
    for (var n = el; n && n.nodeType === 1; n = n.parentElement) {
      if (n.hasAttribute("data-i18n-skip")) return true;
      var t = n.tagName;
      if (t === "SCRIPT" || t === "STYLE" || t === "TEXTAREA" || t === "CODE" ||
          t === "PRE") return true;
    }
    return false;
  }

  function walkText(root, d) {
    var w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function (n) {
        if (!n.nodeValue || !n.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
        if (skip(n.parentElement)) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    var node, list = [];
    while ((node = w.nextNode())) list.push(node);
    list.forEach(function (n) {
      if (!origText.has(n)) origText.set(n, n.nodeValue);
      var src = origText.get(n);
      var out = d ? tr(src, d) : src;
      if (n.nodeValue !== out) n.nodeValue = out;
    });
  }

  function walkAttrs(root, d) {
    var sel = ATTRS.map(function (a) { return "[" + a + "]"; }).join(",");
    var els = [].slice.call(root.querySelectorAll(sel));
    if (root.nodeType === 1 && root.matches && root.matches(sel)) els.unshift(root);
    els.forEach(function (el) {
      if (skip(el)) return;
      ATTRS.forEach(function (a) {
        if (!el.hasAttribute(a)) return;
        var keep = "i18nO" + a.replace(/-/g, "");
        if (el.dataset[keep] === undefined) el.dataset[keep] = el.getAttribute(a);
        var src = el.dataset[keep];
        var out = d ? tr(src, d) : src;
        if (el.getAttribute(a) !== out) el.setAttribute(a, out);
      });
    });
  }

  function apply(root) {
    root = root || document.body;
    if (!root) return;
    var d = dict();
    if (root.nodeType === 1 || root.nodeType === 9 || root.nodeType === 11) {
      walkText(root, d);
      walkAttrs(root, d);
    }
    document.documentElement.lang = current;
  }

  /* Re-translate anything the app renders after load. Debounced, and it turns
     itself off while it writes so it cannot answer its own mutations. */
  function watch() {
    if (observer || !window.MutationObserver || !document.body) return;
    observer = new MutationObserver(function (muts) {
      if (current === "en" && !window.I18N_LANGS.en) return;
      var roots = [];
      muts.forEach(function (m) {
        if (m.type === "childList") {
          [].forEach.call(m.addedNodes, function (n) {
            if (n.nodeType === 1) roots.push(n);
            else if (n.nodeType === 3 && n.parentElement) roots.push(n.parentElement);
          });
        }
      });
      if (!roots.length) return;
      clearTimeout(pending);
      pending = setTimeout(function () {
        observer.disconnect();
        try { roots.forEach(function (r) { if (r.isConnected) apply(r); }); }
        finally { observer.observe(document.body, {childList: true, subtree: true}); }
      }, 30);
    });
    observer.observe(document.body, {childList: true, subtree: true});
  }

  function set(lang) {
    if (!window.I18N_LANGS || (lang !== "en" && !window.I18N_LANGS[lang])) lang = "en";
    current = lang;
    try { localStorage.setItem(STORE, lang); } catch (e) { /* private window */ }
    if (observer) observer.disconnect();
    apply(document.body);
    if (observer) observer.observe(document.body, {childList: true, subtree: true});
    var sel = document.getElementById("lang-sel");
    if (sel && sel.value !== lang) sel.value = lang;
  }

  function init() {
    var saved = null;
    try { saved = localStorage.getItem(STORE); } catch (e) { /* ignore */ }
    if (!saved) {
      // Fall back to the browser's own preference, so Yasharan gets Spanish
      // without being told there is a menu.
      var nav = (navigator.languages || [navigator.language || "en"])[0] || "en";
      var two = nav.slice(0, 2).toLowerCase();
      if (window.I18N_LANGS && window.I18N_LANGS[two]) saved = two;
    }
    current = (saved && (saved === "en" || (window.I18N_LANGS || {})[saved])) ? saved : "en";

    var sel = document.getElementById("lang-sel");
    if (sel) {
      sel.value = current;
      sel.addEventListener("change", function () { set(sel.value); });
    }
    apply(document.body);
    watch();
  }

  window.I18N = {apply: apply, set: set, get lang() { return current; }};

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
