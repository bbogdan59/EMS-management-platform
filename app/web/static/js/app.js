function emsToggleTheme() {
  const root = document.documentElement;
  const isDark = root.classList.toggle("dark");
  try {
    localStorage.setItem("ems-theme", isDark ? "dark" : "light");
  } catch (e) {
    /* localStorage indisponibil (mod privat) -- tema ramane doar pentru sesiunea curenta */
  }
}

/**
 * Copiaza textul unui element (de regula un link de invitatie afisat o
 * singura data, issue #149) folosind Clipboard API, cu fallback prin
 * execCommand pentru browsere/contexte fara acces la API modern (ex. pagina
 * servita non-HTTPS in dezvoltare locala). Valoarea NU e trimisa niciodata
 * catre server -- doar copiata local, in clipboard-ul utilizatorului.
 * `buttonEl` primeste `data-copy-target="<id element sursa>"` si o
 * confirmare vizuala temporara la succes.
 */
function emsCopyToClipboard(buttonEl) {
  const sourceId = buttonEl.getAttribute("data-copy-target");
  const sourceEl = sourceId ? document.getElementById(sourceId) : null;
  const text = sourceEl ? sourceEl.textContent : "";
  if (!text) return;

  const showConfirmation = () => {
    const original = buttonEl.textContent;
    buttonEl.textContent = "Copiat!";
    buttonEl.disabled = true;
    setTimeout(() => {
      buttonEl.textContent = original;
      buttonEl.disabled = false;
    }, 2000);
  };

  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(showConfirmation, () => _emsCopyFallback(text, showConfirmation));
  } else {
    _emsCopyFallback(text, showConfirmation);
  }
}

function _emsCopyFallback(text, onSuccess) {
  const helper = document.createElement("textarea");
  helper.value = text;
  helper.setAttribute("readonly", "");
  helper.style.position = "fixed";
  helper.style.opacity = "0";
  document.body.appendChild(helper);
  helper.select();
  helper.setSelectionRange(0, text.length);
  try {
    if (document.execCommand("copy")) onSuccess();
  } catch (e) {
    /* copierea a esuat -- utilizatorul poate selecta manual textul afisat */
  }
  document.body.removeChild(helper);
}

/**
 * Conexiune SSE cu reconectare automata si backoff exponential plafonat.
 * Foloseste EventSource nativ; la eroare/inchidere reincearca la 1s, 2s, 4s...
 * pana la maxDelayMs, apoi ramane pe acel interval.
 */
function emsConnectSSE(url, handlers) {
  let delay = 1000;
  const maxDelay = 30000;
  let source = null;
  let closedByUser = false;

  function connect() {
    source = new EventSource(url);
    source.onopen = () => {
      delay = 1000;
      if (handlers.onopen) handlers.onopen();
    };
    for (const [eventName, fn] of Object.entries(handlers.events || {})) {
      source.addEventListener(eventName, (ev) => fn(ev));
    }
    source.onerror = () => {
      if (handlers.onerror) handlers.onerror();
      source.close();
      if (!closedByUser) {
        setTimeout(connect, delay);
        delay = Math.min(delay * 2, maxDelay);
      }
    };
  }

  connect();

  return {
    close() {
      closedByUser = true;
      if (source) source.close();
    },
  };
}
